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
    assert "os.kill" not in (repository_root / "docker-compose.yml").read_text()
    assert "@sha256:" in compose["services"]["ollama"]["image"]
    assert all(
        "worker_healthcheck" in " ".join(services[name]["healthcheck"]["test"])
        for name in ("inference-worker", "ingestion-worker", "indexing-worker", "scheduler")
    )


def test_default_compose_warms_embedding_before_api_can_start():
    repository_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repository_root / "docker-compose.yml").read_text())
    embedding = compose["services"]["embedding-service"]
    api = compose["services"]["api"]

    assert embedding["environment"]["EMBEDDING_WARMUP"] == "${EMBEDDING_WARMUP:-true}"
    assert "/ready" in " ".join(embedding["healthcheck"]["test"])
    assert embedding["healthcheck"]["start_period"] == ("${EMBEDDING_HEALTH_START_PERIOD:-30m}")
    assert api["depends_on"]["embedding-service"]["condition"] == "service_healthy"


def test_helm_uses_split_immutable_images_and_restricted_networking():
    root = Path(__file__).resolve().parents[1] / "deploy/helm/rag-assistant"
    values = yaml.safe_load((root / "values.yaml").read_text())
    for key in ("commonImage", "ingestionImage", "webImage", "embeddingImage"):
        assert values[key]["digest"].startswith("sha256:")
    deployments = (root / "templates/deployments.yaml").read_text()
    policies = (root / "templates/policies.yaml").read_text()
    ingress = (root / "templates/ingress.yaml").read_text()
    assert "ingestionImage" in deployments
    assert "worker_healthcheck" in deployments
    assert "os.kill" not in deployments
    assert "namespaceSelector: {}" not in policies
    assert "privateEndpointCidrs" in policies
    assert "/metrics" not in ingress
