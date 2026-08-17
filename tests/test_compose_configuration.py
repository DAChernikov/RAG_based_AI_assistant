from pathlib import Path

import yaml


def test_compose_has_final_workers_without_legacy_artifact_mount():
    repository_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repository_root / "docker-compose.yml").read_text())
    services = compose["services"]
    assert {
        "api",
        "inference-worker",
        "ingestion-worker",
        "indexing-worker",
        "scheduler",
        "web",
    } <= set(services)
    worker = services["inference-worker"]
    assert "volumes" not in worker
    assert "ARTIFACTS_DIR" not in worker["environment"]
    ingestion_worker = compose["services"]["ingestion-worker"]
    assert ingestion_worker["build"]["dockerfile"] == "infra/ingestion-worker.Dockerfile"
    assert compose["services"]["postgres"]["image"].startswith("pgvector/")
