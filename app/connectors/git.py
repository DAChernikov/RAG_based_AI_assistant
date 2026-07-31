from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import os
import re
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

    async def discover(
        self,
        config: GitSourceConfig,
        previous: dict[str, PreviousObject] | None = None,
        *,
        previous_revision: str | None = None,
    ) -> DiscoveryResult:
        previous = previous or {}
        credentials = (
            await self.credential_resolver.resolve(config.credential_ref)
            if config.credential_ref
            else CredentialMaterial()
        )
        environment = os.environ.copy()
        environment.update(credentials.git_environment)
        environment.update(
            {
                "GIT_LFS_SKIP_SMUDGE": "1",
                "GIT_TERMINAL_PROMPT": "0",
                "GIT_CONFIG_NOSYSTEM": "1",
            }
        )
        with tempfile.TemporaryDirectory(prefix="rag-git-") as temporary:
            root = Path(temporary)
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
