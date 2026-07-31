import pytest
from pydantic import ValidationError

from app.catalog.configs import (
    GitSourceConfig,
    JDBCSourceConfig,
    WebsiteSourceConfig,
    parse_source_config,
)
from app.catalog.jdbc_urls import parse_jdbc_url


def test_typed_website_git_and_jdbc_configs():
    website = parse_source_config(
        {
            "source_type": "website",
            "root_url": "https://docs.example.com/start",
            "allowed_domains": ["example.com"],
            "include_patterns": ["/docs/**"],
            "exclude_patterns": ["/private/**"],
            "max_pages": 100,
            "max_depth": 4,
            "credential_ref": "cred:website/docs",
        }
    )
    git = parse_source_config(
        {
            "source_type": "git",
            "repository_url": "https://git.example.com/team/project.git",
            "ref_kind": "commit",
            "ref": "abc123",
            "include_patterns": ["src/**"],
            "credential_ref": "cred:git/read-only",
        }
    )
    jdbc = parse_source_config(
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:neon-demo",
            "jdbc_url": "jdbc:postgresql://db.example.com/demo?sslmode=verify-full",
            "host_allowlist": ["db.example.com"],
            "database_allowlist": ["demo"],
            "catalog_allowlist": ["demo"],
            "schema_allowlist": ["rag_demo_source"],
            "metadata_policy": {"include_indexes": False},
        }
    )

    assert isinstance(website, WebsiteSourceConfig)
    assert isinstance(git, GitSourceConfig)
    assert isinstance(jdbc, JDBCSourceConfig)
    assert website.config_version == git.config_version == jdbc.config_version == "1.0"


@pytest.mark.parametrize(
    "repository_url",
    [
        "https://git.example.com/team/project.git",
        "ssh://git@git.example.com/team/project.git",
        "git@git.example.com:team/project.git",
    ],
)
def test_git_config_accepts_supported_repository_urls(repository_url):
    config = parse_source_config(
        {
            "source_type": "git",
            "repository_url": repository_url,
            "ref_kind": "branch",
            "ref": "main",
        }
    )
    assert config.repository_url == repository_url


@pytest.mark.parametrize(
    "payload",
    [
        {
            "source_type": "website",
            "root_url": "https://docs.example.com",
            "allowed_domains": ["example.com"],
            "password": "plaintext",
        },
        {
            "source_type": "website",
            "root_url": "https://user:password@docs.example.com",
            "allowed_domains": ["example.com"],
        },
        {
            "source_type": "website",
            "root_url": "https://docs.example.com",
            "allowed_domains": ["example.com"],
            "credential_ref": "plaintext-token-value",
        },
        {
            "source_type": "git",
            "repository_url": "https://user:token@git.example.com/project.git",
        },
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:demo",
            "jdbc_url": "jdbc:postgresql://user:password@db.example.com/demo",
            "schema_allowlist": ["public"],
        },
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:demo",
            "jdbc_url": "jdbc:postgresql://db.example.com/demo?password=plaintext",
            "schema_allowlist": ["public"],
        },
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:demo",
            "jdbc_url": "jdbc:postgresql://db.example.com/demo?sslpassword=plaintext",
            "schema_allowlist": ["public"],
        },
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:demo",
            "jdbc_url": "jdbc:postgresql://db.example.com/demo?access_token=plaintext",
            "schema_allowlist": ["public"],
        },
        {
            "source_type": "jdbc",
            "driver_id": "postgresql",
            "connection_ref": "connection:demo",
            "jdbc_url": "jdbc:postgresql://db.example.com/demo?apiKey=plaintext",
            "schema_allowlist": ["public"],
        },
    ],
)
def test_source_configs_reject_plaintext_secrets(payload):
    with pytest.raises(ValidationError):
        parse_source_config(payload)


def test_website_root_must_be_in_allowed_domains():
    with pytest.raises(ValidationError, match="root_url host"):
        parse_source_config(
            {
                "source_type": "website",
                "root_url": "https://outside.example.net",
                "allowed_domains": ["example.com"],
            }
        )


@pytest.mark.parametrize(
    "jdbc_url",
    [
        "jdbc:postgresql://user@db.example.test/demo?sslmode=verify-full",
        "jdbc:postgresql://db.example.test/demo?user=blocked",
        "jdbc:postgresql://db.example.test/demo?p%61ssword=blocked",
        "jdbc:sqlserver://db.example.test;databaseName=demo;password=blocked",
        "jdbc:sqlserver://db.example.test;databaseName=demo;access_token=blocked",
        "jdbc:sqlserver://db.example.test;databaseName=demo;apiKey=blocked",
        "jdbc:oracle:thin:user/blocked@//db.example.test:1521/demo",
        "jdbc:oracle:thin:@(DESCRIPTION=(PASSWORD=blocked)(HOST=db.example.test))",
        "jdbc:oracle:thin:@(DESCRIPTION=(USER=blocked)(HOST=db.example.test))",
    ],
)
def test_jdbc_url_parser_rejects_credentials_across_vendor_syntaxes(jdbc_url):
    with pytest.raises(ValueError):
        parse_jdbc_url(jdbc_url)


def test_jdbc_url_parser_extracts_safe_postgresql_endpoint():
    parsed = parse_jdbc_url("jdbc:postgresql://db.example.test:5432/demo?sslmode=verify-full")
    assert parsed.host == "db.example.test"
    assert parsed.port == 5432
    assert parsed.database == "demo"
    assert parsed.properties == {"sslmode": "verify-full"}


def _jdbc_config(**overrides):
    payload = {
        "source_type": "jdbc",
        "driver_id": "postgresql",
        "connection_ref": "connection:neon-demo",
        "jdbc_url": "jdbc:postgresql://db.example.test/demo?sslmode=verify-full",
        "host_allowlist": ["db.example.test"],
        "database_allowlist": ["demo"],
        "catalog_allowlist": ["demo"],
        "schema_allowlist": ["rag_demo_source"],
    }
    payload.update(overrides)
    return payload


@pytest.mark.parametrize(
    "overrides",
    [
        {"driver_id": "uploaded-jar"},
        {"jdbc_url": "jdbc:postgresql://other.example.test/demo?sslmode=verify-full"},
        {"jdbc_url": "jdbc:postgresql://db.example.test/other?sslmode=verify-full"},
        {"jdbc_url": "jdbc:postgresql://db.example.test/demo?sslmode=require"},
        {
            "jdbc_url": (
                "jdbc:postgresql://db.example.test/demo?" "sslmode=verify-full&connectTimeout=100"
            )
        },
    ],
)
def test_jdbc_config_enforces_registry_target_allowlists_and_tls(overrides):
    with pytest.raises(ValidationError):
        parse_source_config(_jdbc_config(**overrides))
