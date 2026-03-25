from pathlib import Path

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
