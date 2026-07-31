import pytest
from pydantic import ValidationError

from app.catalog.configs import (
    GitSourceConfig,
    JDBCSourceConfig,
    WebsiteSourceConfig,
    parse_source_config,
)


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
            "driver_id": "postgresql-42.7",
            "connection_ref": "connection:neon-demo",
            "jdbc_url": "jdbc:postgresql://db.example.com/demo?sslmode=require",
            "schema_allowlist": ["rag_demo_source"],
            "metadata_policy": {"include_indexes": False},
            "credential_ref": "cred:jdbc/neon-demo",
        }
    )

    assert isinstance(website, WebsiteSourceConfig)
    assert isinstance(git, GitSourceConfig)
    assert isinstance(jdbc, JDBCSourceConfig)
    assert website.config_version == git.config_version == jdbc.config_version == "1.0"


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
