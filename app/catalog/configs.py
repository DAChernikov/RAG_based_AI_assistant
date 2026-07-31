from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import parse_qsl, urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

_CREDENTIAL_REF = re.compile(
    r"^(?:cred|credential|vault|secretref|connection):[A-Za-z0-9_.:/-]{1,240}$"
)
_SECRET_KEYS = {
    "password",
    "passwd",
    "token",
    "secret",
    "apikey",
    "privatekey",
    "sslpassword",
    "accesstoken",
    "refreshtoken",
    "clientsecret",
}


def _is_secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", value.casefold())
    return normalized in _SECRET_KEYS or any(
        marker in normalized for marker in ("password", "token", "secret", "apikey")
    )


def _reject_secret_fields(value: Any, path: str = "config") -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if _is_secret_key(str(key)):
                raise ValueError(f"Plaintext secret field is forbidden at {path}.{key}.")
            _reject_secret_fields(child, f"{path}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _reject_secret_fields(child, f"{path}[{index}]")


class SourceConfigBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    config_version: Literal["1.0"] = "1.0"
    source_type: Literal["website", "git", "jdbc"]
    credential_ref: str | None = None

    @model_validator(mode="before")
    @classmethod
    def reject_plaintext_secrets(cls, value):
        _reject_secret_fields(value)
        return value

    @field_validator("credential_ref")
    @classmethod
    def validate_credential_ref(cls, value: str | None) -> str | None:
        if value is not None and not _CREDENTIAL_REF.fullmatch(value):
            raise ValueError("credential_ref must be an opaque reference, not a credential.")
        return value


class WebsiteSourceConfig(SourceConfigBase):
    source_type: Literal["website"] = "website"
    root_url: AnyHttpUrl
    allowed_domains: list[str] = Field(min_length=1, max_length=100)
    include_patterns: list[str] = Field(default_factory=list, max_length=100)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=100)
    max_pages: int = Field(default=1000, ge=1, le=100_000)
    max_depth: int = Field(default=10, ge=0, le=100)

    @field_validator("allowed_domains")
    @classmethod
    def normalize_domains(cls, values: list[str]) -> list[str]:
        normalized = []
        for value in values:
            domain = value.strip().lower().rstrip(".")
            if not domain or "://" in domain or "/" in domain or "@" in domain:
                raise ValueError("allowed_domains entries must be hostnames.")
            normalized.append(domain)
        return sorted(set(normalized))

    @field_validator("root_url")
    @classmethod
    def reject_root_userinfo(cls, value: AnyHttpUrl) -> AnyHttpUrl:
        if value.username is not None or value.password is not None:
            raise ValueError("root_url must not contain embedded credentials.")
        return value

    @model_validator(mode="after")
    def root_must_be_allowed(self):
        host = (self.root_url.host or "").lower().rstrip(".")
        if not any(
            host == domain or host.endswith(f".{domain}") for domain in self.allowed_domains
        ):
            raise ValueError("root_url host must be covered by allowed_domains.")
        return self


class GitSourceConfig(SourceConfigBase):
    source_type: Literal["git"] = "git"
    repository_url: str = Field(min_length=3, max_length=2000)
    ref_kind: Literal["branch", "tag", "commit"] = "branch"
    ref: str = Field(default="main", min_length=1, max_length=255)
    is_group: bool = False
    include_patterns: list[str] = Field(default_factory=list, max_length=200)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=200)

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        raw = value.strip()
        if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", raw):
            return raw
        parsed = urlsplit(raw)
        if parsed.scheme not in {"http", "https", "ssh"} or not parsed.hostname:
            raise ValueError("repository_url must use HTTPS, ssh:// or git@host:path syntax.")
        if parsed.password is not None or (
            parsed.scheme in {"http", "https"} and parsed.username is not None
        ):
            raise ValueError("repository_url must not contain embedded credentials.")
        return raw


class JDBCMetadataPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid")

    include_tables: bool = True
    include_views: bool = True
    include_columns: bool = True
    include_constraints: bool = True
    include_indexes: bool = True
    include_comments: bool = True


class JDBCSourceConfig(SourceConfigBase):
    source_type: Literal["jdbc"] = "jdbc"
    driver_id: str = Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_.-]+$")
    connection_ref: str = Field(min_length=3, max_length=255)
    jdbc_url: str | None = Field(default=None, max_length=2000)
    catalog_allowlist: list[str] = Field(default_factory=list, max_length=100)
    schema_allowlist: list[str] = Field(min_length=1, max_length=100)
    metadata_policy: JDBCMetadataPolicy = Field(default_factory=JDBCMetadataPolicy)

    @field_validator("connection_ref")
    @classmethod
    def validate_connection_ref(cls, value: str) -> str:
        if not _CREDENTIAL_REF.fullmatch(value):
            raise ValueError("connection_ref must be an opaque reference.")
        return value

    @field_validator("jdbc_url")
    @classmethod
    def reject_embedded_jdbc_secret(cls, value: str | None) -> str | None:
        if value is None:
            return None
        raw = value.strip()
        if not raw.lower().startswith("jdbc:"):
            raise ValueError("jdbc_url must start with jdbc:.")
        parsed = urlsplit(raw[5:])
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("jdbc_url must not contain embedded credentials.")
        sensitive_query_keys = {key for key, _ in parse_qsl(parsed.query, keep_blank_values=True)}
        if any(_is_secret_key(key) for key in sensitive_query_keys):
            raise ValueError("jdbc_url must not contain credential query parameters.")
        return raw


SourceConfig = Annotated[
    WebsiteSourceConfig | GitSourceConfig | JDBCSourceConfig,
    Field(discriminator="source_type"),
]
SOURCE_CONFIG_ADAPTER = TypeAdapter(SourceConfig)


def parse_source_config(value: dict[str, Any]) -> SourceConfig:
    return SOURCE_CONFIG_ADAPTER.validate_python(value)
