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
    assert worker["volumes"] == ["secret_keys:/var/lib/rag-secrets"]
    assert "ARTIFACTS_DIR" not in worker["environment"]
    ingestion_worker = compose["services"]["ingestion-worker"]
    assert ingestion_worker["build"]["dockerfile"] == "infra/ingestion-worker.Dockerfile"
    assert compose["services"]["postgres"]["image"].startswith("pgvector/")
    assert "os.kill" not in (repository_root / "docker-compose.yml").read_text()
    assert "@sha256:" in compose["services"]["ollama"]["image"]
    assert all(
        "worker_healthcheck" in " ".join(services[name]["healthcheck"]["test"])
        for name in ("inference-worker", "ingestion-worker", "indexing-worker", "scheduler", "bot")
    )
    assert "profiles" not in services["bot"]
    assert services["bot"]["volumes"] == ["secret_keys:/var/lib/rag-secrets"]


def test_default_compose_warms_embedding_without_blocking_setup_ui():
    repository_root = Path(__file__).resolve().parents[1]
    compose = yaml.safe_load((repository_root / "docker-compose.yml").read_text())
    embedding = compose["services"]["embedding-service"]
    api = compose["services"]["api"]

    assert embedding["environment"]["EMBEDDING_WARMUP"] == "${EMBEDDING_WARMUP:-true}"
    assert "/ready" in " ".join(embedding["healthcheck"]["test"])
    assert embedding["healthcheck"]["start_period"] == ("${EMBEDDING_HEALTH_START_PERIOD:-30m}")
    assert api["depends_on"]["embedding-service"]["condition"] == "service_started"
    assert compose["services"]["web"]["ports"] == [
        "${WEB_BIND_ADDRESS:-127.0.0.1}:${WEB_PORT:-8080}:8080"
    ]
    assert api["volumes"] == ["secret_keys:/var/lib/rag-secrets"]
    assert api["environment"]["SECRET_STORE_KEY_FILE"] == "/var/lib/rag-secrets/master.key"


def test_platform_scripts_create_local_config_and_use_the_ui_first_command():
    root = Path(__file__).resolve().parents[1]
    shell = (root / "scripts/dev-up").read_text()
    powershell = (root / "scripts/dev-up.ps1").read_text()
    for script in (shell, powershell):
        assert ".env.example" in script and ".env.local" in script
        assert "docker compose --env-file .env.local up -d --build" in script
        assert "/health" in script
    assert (root / "scripts/dev-up").stat().st_mode & 0o111


def test_production_compose_mounts_credentials_as_files():
    root = Path(__file__).resolve().parents[1]
    override = yaml.safe_load((root / "compose.production.yml").read_text())
    api = override["services"]["api"]
    assert api["environment"]["SECRET_STORE_MASTER_KEY_FILE"].startswith("/run/secrets/")
    assert api["environment"]["SETUP_BOOTSTRAP_TOKEN_FILE"].startswith("/run/secrets/")
    assert "SECRET_STORE_MASTER_KEY" not in api["environment"]
    assert "SETUP_BOOTSTRAP_TOKEN" not in api["environment"]
    assert set(api["secrets"]) == {"secret_store_master_key", "setup_bootstrap_token"}
    for name in (
        "migrate",
        "api",
        "inference-worker",
        "ingestion-worker",
        "indexing-worker",
        "scheduler",
        "bot",
    ):
        service = override["services"][name]
        assert service["environment"]["SECRET_STORE_MASTER_KEY_FILE"].startswith("/run/secrets/")
        assert "secret_store_master_key" in service["secrets"]


def test_helm_uses_split_immutable_images_and_restricted_networking():
    root = Path(__file__).resolve().parents[1] / "deploy/helm/rag-assistant"
    values = yaml.safe_load((root / "values.yaml").read_text())
    for key in ("commonImage", "ingestionImage", "webImage", "embeddingImage", "botImage"):
        assert values[key]["digest"].startswith("sha256:")
    deployments = (root / "templates/deployments.yaml").read_text()
    policies = (root / "templates/policies.yaml").read_text()
    ingress = (root / "templates/ingress.yaml").read_text()
    assert "ingestionImage" in deployments
    assert "botImage" in deployments
    assert "Keep Setup Wizard routable" in deployments
    assert "worker_healthcheck" in deployments
    assert "os.kill" not in deployments
    assert "namespaceSelector: {}" not in policies
    assert "privateEndpointCidrs" in policies
    assert "/metrics" not in ingress
