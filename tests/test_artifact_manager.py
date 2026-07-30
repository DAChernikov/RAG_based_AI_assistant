import json
import zipfile
from pathlib import Path

import joblib
import numpy as np
import pytest

from app.api.services.artifact_manager import ArtifactManager


def test_has_required_artifacts_false_when_empty(tmp_path: Path):
    manager = ArtifactManager(str(tmp_path))
    assert manager.has_required_artifacts() is False


def test_has_required_artifacts_true_when_required_files_exist(tmp_path: Path):
    required_files = [
        "corpus_emb.npy",
        "corpus.joblib",
        "meta.json",
        "retriever_model/config.json",
        "retriever_model/config_sentence_transformers.json",
    ]

    for rel_path in required_files:
        path = tmp_path / rel_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("stub", encoding="utf-8")

    manager = ArtifactManager(str(tmp_path))
    assert manager.has_required_artifacts() is True


def _write_valid_artifacts(root: Path, marker: str = "new") -> None:
    root.mkdir(parents=True, exist_ok=True)
    np.save(root / "corpus_emb.npy", np.array([[1.0, 0.0]], dtype=np.float32))
    joblib.dump([{"doc_id": marker, "text": marker}], root / "corpus.joblib")
    (root / "meta.json").write_text(json.dumps({"marker": marker}), encoding="utf-8")
    model_dir = root / "retriever_model"
    model_dir.mkdir()
    (model_dir / "config.json").write_text("{}", encoding="utf-8")
    (model_dir / "config_sentence_transformers.json").write_text("{}", encoding="utf-8")


def _zip_directory(source: Path, archive: Path) -> None:
    with zipfile.ZipFile(archive, "w") as file:
        for path in source.rglob("*"):
            if path.is_file():
                file.write(path, path.relative_to(source).as_posix())


class FakeS3Client:
    def __init__(self, archive: Path):
        self.archive = archive
        self.calls = 0

    def download_file(self, bucket, key, destination, *, max_bytes):
        self.calls += 1
        Path(destination).write_bytes(self.archive.read_bytes())


def test_local_artifacts_do_not_initialize_s3(tmp_path: Path, monkeypatch):
    _write_valid_artifacts(tmp_path)
    factory_called = False

    def factory():
        nonlocal factory_called
        factory_called = True
        raise AssertionError("S3 must stay lazy")

    monkeypatch.setattr(
        "app.api.services.artifact_manager.settings.force_artifacts_download", False
    )
    ArtifactManager(str(tmp_path), s3_client_factory=factory).prepare()

    assert factory_called is False


def test_safe_download_activates_artifacts_atomically(tmp_path: Path, monkeypatch):
    active = tmp_path / "active"
    source = tmp_path / "source"
    archive = tmp_path / "bundle.zip"
    _write_valid_artifacts(active, marker="old")
    _write_valid_artifacts(source, marker="new")
    _zip_directory(source, archive)
    fake_s3 = FakeS3Client(archive)
    monkeypatch.setattr("app.api.services.artifact_manager.settings.force_artifacts_download", True)

    ArtifactManager(str(active), s3_client=fake_s3).prepare()

    corpus = joblib.load(active / "corpus.joblib")
    assert corpus[0]["doc_id"] == "new"
    assert fake_s3.calls == 1
    assert not list(tmp_path.glob(".active.staging-*"))
    assert not list(tmp_path.glob(".active.backup-*"))


@pytest.mark.parametrize("unsafe_name", ["../escape.txt", "/absolute.txt", "..\\escape.txt"])
def test_unsafe_zip_is_rejected_and_active_artifacts_survive(
    tmp_path: Path,
    monkeypatch,
    unsafe_name: str,
):
    active = tmp_path / "active"
    archive = tmp_path / "unsafe.zip"
    _write_valid_artifacts(active, marker="old")
    with zipfile.ZipFile(archive, "w") as file:
        file.writestr(unsafe_name, "unsafe")
    monkeypatch.setattr("app.api.services.artifact_manager.settings.force_artifacts_download", True)

    with pytest.raises(RuntimeError, match="unsafe"):
        ArtifactManager(str(active), s3_client=FakeS3Client(archive)).prepare()

    corpus = joblib.load(active / "corpus.joblib")
    assert corpus[0]["doc_id"] == "old"


def test_missing_s3_configuration_has_sanitized_error(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(
        "app.api.services.artifact_manager.settings.force_artifacts_download", False
    )
    monkeypatch.setattr("app.api.services.artifact_manager.settings.s3_endpoint_url", None)
    monkeypatch.setattr("app.api.services.artifact_manager.settings.s3_bucket", None)
    monkeypatch.setattr("app.api.services.artifact_manager.settings.aws_access_key_id", None)
    monkeypatch.setattr("app.api.services.artifact_manager.settings.aws_secret_access_key", None)

    with pytest.raises(RuntimeError, match="configuration is incomplete"):
        ArtifactManager(str(tmp_path / "missing")).prepare()


def test_activation_failure_rolls_back_previous_artifacts(tmp_path: Path, monkeypatch):
    active = tmp_path / "active"
    source = tmp_path / "source"
    archive = tmp_path / "bundle.zip"
    _write_valid_artifacts(active, marker="old")
    _write_valid_artifacts(source, marker="new")
    _zip_directory(source, archive)
    monkeypatch.setattr("app.api.services.artifact_manager.settings.force_artifacts_download", True)
    original_replace = __import__("os").replace
    calls = 0

    def fail_candidate_activation(source_path, destination_path):
        nonlocal calls
        calls += 1
        if calls == 2:
            raise OSError("simulated activation failure")
        return original_replace(source_path, destination_path)

    monkeypatch.setattr("app.api.services.artifact_manager.os.replace", fail_candidate_activation)

    with pytest.raises(OSError, match="simulated"):
        ArtifactManager(str(active), s3_client=FakeS3Client(archive)).prepare()

    assert joblib.load(active / "corpus.joblib")[0]["doc_id"] == "old"
