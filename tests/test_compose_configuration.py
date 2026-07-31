from pathlib import Path

import yaml


def test_worker_artifact_directory_matches_compose_mount():
    repository_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repository_root / "docker-compose.yml").read_text())
    worker = compose["services"]["worker"]

    assert worker["environment"]["ARTIFACTS_DIR"] == "/app/artifacts/artifacts_rag_baseline_latest"
    assert "./artifacts:/app/artifacts" in worker["volumes"]
