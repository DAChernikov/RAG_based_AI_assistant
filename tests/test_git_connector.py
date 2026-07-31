from __future__ import annotations

import subprocess

import pytest

from app.catalog.configs import GitSourceConfig
from app.connectors.base import (
    CredentialIsolationError,
    CredentialMaterial,
    CredentialResolver,
    PreviousObject,
)
from app.connectors.git import GitConnector


class FakeCredentialResolver(CredentialResolver):
    async def resolve(self, reference: str) -> CredentialMaterial:
        return CredentialMaterial(git_environment={})


def git(path, *args):
    return subprocess.run(
        ["git", *args], cwd=path, check=True, text=True, capture_output=True
    ).stdout.strip()


def make_config(repository, commit):
    return GitSourceConfig.model_construct(
        source_type="git",
        config_version="1.0",
        credential_ref=None,
        repository_url=str(repository),
        ref_kind="commit",
        ref=commit,
        is_group=False,
        include_patterns=[],
        exclude_patterns=[],
        max_files=100,
        max_file_bytes=100_000,
        max_total_bytes=1_000_000,
        include_submodules=False,
        enable_lfs=False,
    )


@pytest.mark.asyncio
async def test_git_incremental_rename_delete_and_structure_parsing(tmp_path):
    repository = tmp_path / "origin"
    repository.mkdir()
    git(repository, "init", "--quiet")
    git(repository, "config", "user.email", "test@example.test")
    git(repository, "config", "user.name", "Test")
    (repository / "README.md").write_text("# Intro\nFirst\n")
    (repository / "module.py").write_text("def answer():\n    return 42\n")
    (repository / ".env").write_text("TOKEN=never-ingest")
    git(repository, "add", ".")
    git(repository, "commit", "--quiet", "-m", "first")
    submodule_sha = git(repository, "rev-parse", "HEAD")
    git(
        repository,
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{submodule_sha},external/dependency",
    )
    (repository / "large.bin").write_text(
        "version https://git-lfs.github.com/spec/v1\n" "oid sha256:" + "a" * 64 + "\nsize 1000000\n"
    )
    git(repository, "add", "large.bin")
    git(repository, "commit", "--quiet", "-m", "gitlink and lfs pointer")
    first_sha = git(repository, "rev-parse", "HEAD")

    connector = GitConnector(FakeCredentialResolver())
    first = await connector.discover(make_config(repository, first_sha))
    assert {item.object_key for item in first.documents} == {"README.md", "module.py"}
    python = next(item for item in first.documents if item.object_key == "module.py")
    assert python.metadata["language"] == "python"
    assert python.chunks[0].metadata["symbol"] == "answer"
    assert python.chunks[0].metadata["line_start"] == 1

    previous = {
        item.object_key: PreviousObject(item.object_key, item.checksum, item.metadata)
        for item in first.documents
    }
    git(repository, "mv", "README.md", "GUIDE.md")
    (repository / "module.py").unlink()
    (repository / "schema.sql").write_text("CREATE TABLE users(id integer);\n")
    git(repository, "add", "-A")
    git(repository, "commit", "--quiet", "-m", "second")
    second_sha = git(repository, "rev-parse", "HEAD")

    second = await connector.discover(
        make_config(repository, second_sha), previous, previous_revision=first_sha
    )
    assert second.renamed == (("README.md", "GUIDE.md"),)
    assert second.deleted == ("module.py",)
    assert second.added == ("schema.sql",)
    assert second.source_revision == second_sha
    sql = next(item for item in second.documents if item.object_key == "schema.sql")
    assert sql.metadata["language"] == "sql"
    assert sql.chunks[0].metadata["symbol"] == "users"


def test_git_environment_isolated_from_host_configuration(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", "/unsafe/host-home")
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/unsafe/global-config")
    environment = GitConnector._isolated_environment(
        tmp_path,
        CredentialMaterial(
            git_environment={"GIT_USERNAME": "test-user", "GIT_PASSWORD": "test-password"}
        ),
        "https://git.example.test/team/project.git",
    )
    assert environment["HOME"] != "/unsafe/host-home"
    assert environment["GIT_CONFIG_GLOBAL"] == "/dev/null"
    assert environment["GIT_CONFIG_NOSYSTEM"] == "1"
    assert environment["GIT_ASKPASS"].startswith(str(tmp_path))


@pytest.mark.asyncio
async def test_git_rejects_unapproved_credential_environment(tmp_path):
    class UnsafeResolver(CredentialResolver):
        async def resolve(self, reference: str) -> CredentialMaterial:
            return CredentialMaterial(git_environment={"GIT_SSH_COMMAND": "unsafe"})

    connector = GitConnector(UnsafeResolver())
    config = make_config(tmp_path, "deadbeef").model_copy(
        update={"credential_ref": "credential:git"}
    )
    with pytest.raises(CredentialIsolationError):
        await connector.discover(config)


@pytest.mark.asyncio
async def test_git_rejects_cross_connector_credential_material(tmp_path):
    class CrossChannelResolver(CredentialResolver):
        async def resolve(self, _reference: str) -> CredentialMaterial:
            return CredentialMaterial(http_headers={"Authorization": "Bearer fixture"})

    connector = GitConnector(CrossChannelResolver())
    config = make_config(tmp_path, "deadbeef").model_copy(
        update={"credential_ref": "credential:git"}
    )
    with pytest.raises(CredentialIsolationError, match="non-Git"):
        await connector.discover(config)
