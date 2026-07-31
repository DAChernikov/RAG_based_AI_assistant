from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
import shlex
import tempfile
from pathlib import Path

from app.catalog.configs import GitSourceConfig
from app.connectors.base import (
    ConnectorDocument,
    CredentialMaterial,
    CredentialResolver,
    DiscoveryResult,
    PreviousObject,
    SourceLimitError,
    TransientConnectorError,
    validate_credential_material,
)
from app.connectors.parsing import parse_code

_IGNORED_DIRECTORIES = {
    ".git",
    ".cache",
    ".gradle",
    ".idea",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".tox",
    ".venv",
    ".next",
    "__pycache__",
    "build",
    "coverage",
    "dist",
    "node_modules",
    "target",
    "venv",
    "vendor",
}
_SECRET_NAMES = {
    ".env",
    ".env.local",
    ".netrc",
    "credentials",
    "credentials.json",
    "id_dsa",
    "id_ed25519",
    "id_rsa",
}
_SECRET_SUFFIXES = {".der", ".jks", ".key", ".p12", ".pfx", ".pem"}


class GitConnector:
    connector_version = "git/1.0"

    def __init__(self, credential_resolver: CredentialResolver):
        self.credential_resolver = credential_resolver

    @staticmethod
    async def _git(cwd: Path, env: dict[str, str], *args: str) -> str:
        process = await asyncio.create_subprocess_exec(
            "git",
            "-c",
            "submodule.recurse=false",
            "-c",
            "filter.lfs.smudge=",
            "-c",
            "filter.lfs.required=false",
            "-c",
            "http.followRedirects=false",
            *args,
            cwd=cwd,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await process.communicate()
        if process.returncode:
            raise TransientConnectorError("Git operation failed.")
        return stdout.decode("utf-8", errors="replace").strip()

    @staticmethod
    def _included(path: str, config: GitSourceConfig) -> bool:
        parts = set(Path(path).parts)
        if parts & _IGNORED_DIRECTORIES:
            return False
        name = Path(path).name.casefold()
        if (
            name in _SECRET_NAMES
            or name.startswith(".env.")
            or Path(path).suffix.casefold() in _SECRET_SUFFIXES
        ):
            return False
        if re.search(r"(?:^|[._-])(secret|credentials?)(?:[._-]|$)", name):
            return False
        if config.include_patterns and not any(
            fnmatch.fnmatch(path, pattern) for pattern in config.include_patterns
        ):
            return False
        return not any(fnmatch.fnmatch(path, pattern) for pattern in config.exclude_patterns)

    @staticmethod
    def _isolated_environment(
        root: Path, credentials: CredentialMaterial, repository_url: str
    ) -> dict[str, str]:
        home = root / "home"
        home.mkdir(mode=0o700)
        environment = {
            key: value
            for key in ("PATH", "LANG", "LC_ALL", "SSL_CERT_FILE", "SSL_CERT_DIR")
            if (value := os.environ.get(key))
        }
        environment.update(
            {
                "HOME": str(home),
                "XDG_CONFIG_HOME": str(home / ".config"),
                "GIT_CONFIG_GLOBAL": "/dev/null",
                "GIT_CONFIG_NOSYSTEM": "1",
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_TERMINAL_PROMPT": "0",
            }
        )
        git_credentials = credentials.git_environment
        username = git_credentials.get("GIT_USERNAME")
        password = git_credentials.get("GIT_PASSWORD")
        if bool(username) != bool(password):
            raise RuntimeError("Git username and password must be configured together.")
        if username and password:
            askpass = root / "git-askpass.sh"
            askpass.write_text(
                "#!/bin/sh\n"
                'case "$1" in *Username*) printf "%s" "$GIT_USERNAME" ;; '
                '*) printf "%s" "$GIT_PASSWORD" ;; esac\n'
            )
            askpass.chmod(0o700)
            environment.update(
                {
                    "GIT_ASKPASS": str(askpass),
                    "GIT_USERNAME": username,
                    "GIT_PASSWORD": password,
                }
            )

        is_ssh = repository_url.startswith("ssh://") or repository_url.startswith("git@")
        if is_ssh:
            known_hosts = git_credentials.get("SSH_KNOWN_HOSTS")
            if not known_hosts:
                raise RuntimeError("SSH Git sources require pinned known_hosts material.")
            ssh_dir = root / "ssh"
            ssh_dir.mkdir(mode=0o700)
            known_hosts_path = ssh_dir / "known_hosts"
            known_hosts_path.write_text(known_hosts)
            known_hosts_path.chmod(0o600)
            command = [
                "ssh",
                "-F",
                "/dev/null",
                "-o",
                "IdentitiesOnly=yes",
                "-o",
                "StrictHostKeyChecking=yes",
                "-o",
                f"UserKnownHostsFile={known_hosts_path}",
                "-o",
                "GlobalKnownHostsFile=/dev/null",
            ]
            private_key = git_credentials.get("SSH_PRIVATE_KEY")
            if private_key:
                key_path = ssh_dir / "identity"
                key_path.write_text(private_key)
                key_path.chmod(0o600)
                command.extend(["-i", str(key_path)])
            if socket_path := git_credentials.get("SSH_AUTH_SOCK"):
                environment["SSH_AUTH_SOCK"] = socket_path
            environment["GIT_SSH_COMMAND"] = " ".join(map(shlex.quote, command))
        return environment

    async def discover(
        self,
        config: GitSourceConfig,
        previous: dict[str, PreviousObject] | None = None,
        *,
        previous_revision: str | None = None,
    ) -> DiscoveryResult:
        previous = previous or {}
        credentials = validate_credential_material(
            await self.credential_resolver.resolve(config.credential_ref)
            if config.credential_ref
            else CredentialMaterial()
        )
        with tempfile.TemporaryDirectory(prefix="rag-git-") as temporary:
            root = Path(temporary)
            environment = self._isolated_environment(root, credentials, config.repository_url)
            await self._git(root, environment, "init", "--quiet", "repository")
            repository = root / "repository"
            await self._git(
                repository, environment, "remote", "add", "origin", config.repository_url
            )
            if config.ref_kind == "commit":
                fetch_ref = config.ref
            elif config.ref_kind == "tag":
                fetch_ref = f"refs/tags/{config.ref}"
            else:
                fetch_ref = f"refs/heads/{config.ref}"
            await self._git(
                repository,
                environment,
                "fetch",
                "--quiet",
                "--no-tags" if config.ref_kind != "tag" else "--tags",
                "--depth=1",
                "origin",
                fetch_ref,
            )
            commit_sha = await self._git(
                repository, environment, "rev-parse", "FETCH_HEAD^{commit}"
            )
            await self._git(repository, environment, "checkout", "--quiet", "--detach", commit_sha)
            tracked = (await self._git(repository, environment, "ls-files", "-z")).split("\x00")
            paths = sorted(path for path in tracked if path and self._included(path, config))
            if len(paths) > config.max_files:
                raise SourceLimitError("Git file-count limit exceeded.")

            documents: list[ConnectorDocument] = []
            total_bytes = 0
            for relative in paths:
                path = repository / relative
                # A disabled submodule is represented by a gitlink, not a regular file.
                if not path.is_file():
                    continue
                try:
                    content = path.read_bytes()
                except OSError as exc:
                    raise TransientConnectorError("Git file could not be read.") from exc
                if len(content) > config.max_file_bytes:
                    raise SourceLimitError("Git file-size limit exceeded.")
                total_bytes += len(content)
                if total_bytes > config.max_total_bytes:
                    raise SourceLimitError("Git total-size limit exceeded.")
                if b"\x00" in content[:8192]:
                    continue
                try:
                    text = content.decode("utf-8")
                except UnicodeDecodeError:
                    continue
                if text.startswith("version https://git-lfs.github.com/spec/v1\n"):
                    continue
                checksum = hashlib.sha256(content).hexdigest()
                language, parsed_chunks = parse_code(relative, text)
                chunks = tuple(
                    type(chunk)(
                        text=chunk.text,
                        checksum=chunk.checksum,
                        chunk_index=chunk.chunk_index,
                        metadata={
                            **chunk.metadata,
                            "repository_url": config.repository_url,
                            "ref": config.ref,
                            "commit_sha": commit_sha,
                            "path": relative,
                            "language": language,
                        },
                    )
                    for chunk in parsed_chunks
                )
                documents.append(
                    ConnectorDocument(
                        object_key=relative,
                        canonical_uri=f"git:{config.repository_url}@{commit_sha}:{relative}",
                        title=relative,
                        text=text,
                        checksum=checksum,
                        byte_count=len(content),
                        metadata={
                            "repository_url": config.repository_url,
                            "ref": config.ref,
                            "ref_kind": config.ref_kind,
                            "commit_sha": commit_sha,
                            "path": relative,
                            "language": language,
                        },
                        chunks=chunks,
                    )
                )

        by_key = {document.object_key: document for document in documents}
        added = set(by_key) - set(previous)
        deleted = set(previous) - set(by_key)
        unchanged = {
            key
            for key, document in by_key.items()
            if key in previous and previous[key].checksum == document.checksum
        }
        modified = set(by_key) - added - unchanged
        renamed: list[tuple[str, str]] = []
        deleted_by_checksum = {previous[key].checksum: key for key in deleted}
        for key in tuple(added):
            old_key = deleted_by_checksum.get(by_key[key].checksum)
            if old_key:
                renamed.append((old_key, key))
                added.remove(key)
                deleted.remove(old_key)
        materialized = added | modified | {new for _old, new in renamed}
        return DiscoveryResult(
            documents=tuple(
                document for document in documents if document.object_key in materialized
            ),
            added=tuple(sorted(added)),
            modified=tuple(sorted(modified)),
            unchanged=tuple(sorted(unchanged)),
            deleted=tuple(sorted(deleted)),
            renamed=tuple(sorted(renamed)),
            source_revision=commit_sha,
        )
