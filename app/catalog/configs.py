from __future__ import annotations

import re
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

from pydantic import (
    AnyHttpUrl,
    BaseModel,
    ConfigDict,
    Field,
    TypeAdapter,
    field_validator,
    model_validator,
)

from app.catalog.jdbc_urls import parse_jdbc_url

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
    max_page_bytes: int = Field(default=5_000_000, ge=1024, le=100_000_000)
    max_crawl_seconds: float = Field(default=900.0, gt=0, le=86_400)
    request_timeout_sec: float = Field(default=20.0, gt=0, le=300)
    requests_per_second: float = Field(default=2.0, gt=0, le=100)
    max_retries: int = Field(default=3, ge=0, le=10)
    use_sitemap: bool = True
    credential_allowed_origins: list[str] = Field(default_factory=list, max_length=20)

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
        if self.credential_ref and self.root_url.scheme != "https":
            raise ValueError("Authenticated Website sources must use HTTPS.")
        normalized_origins = []
        for raw in self.credential_allowed_origins:
            parsed = urlsplit(raw.strip())
            origin_host = (parsed.hostname or "").lower().rstrip(".")
            if (
                parsed.scheme != "https"
                or not origin_host
                or parsed.username is not None
                or parsed.password is not None
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
                or not any(
                    origin_host == domain or origin_host.endswith(f".{domain}")
                    for domain in self.allowed_domains
                )
            ):
                raise ValueError(
                    "credential_allowed_origins must contain allowed HTTPS origins only."
                )
            normalized_origins.append(
                f"https://{origin_host}{f':{parsed.port}' if parsed.port else ''}"
            )
        self.credential_allowed_origins = sorted(set(normalized_origins))
        return self


class GitSourceConfig(SourceConfigBase):
    source_type: Literal["git"] = "git"
    repository_url: str = Field(min_length=3, max_length=2000)
    ref_kind: Literal["branch", "tag", "commit"] = "branch"
    ref: str = Field(default="main", min_length=1, max_length=255)
    is_group: bool = False
    include_patterns: list[str] = Field(default_factory=list, max_length=200)
    exclude_patterns: list[str] = Field(default_factory=list, max_length=200)
    max_files: int = Field(default=50_000, ge=1, le=1_000_000)
    max_file_bytes: int = Field(default=5_000_000, ge=1, le=100_000_000)
    max_total_bytes: int = Field(default=2_000_000_000, ge=1, le=20_000_000_000)
    include_submodules: bool = False
    enable_lfs: bool = False

    @field_validator("repository_url")
    @classmethod
    def validate_repository_url(cls, value: str) -> str:
        raw = value.strip()
        if re.fullmatch(r"git@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+", raw):
            return raw
        parsed = urlsplit(raw)
        if parsed.scheme not in {"https", "ssh"} or not parsed.hostname:
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
    driver_registry_version: Literal["1"] = "1"
    connection_ref: str = Field(min_length=3, max_length=255)
    jdbc_url: str = Field(min_length=10, max_length=2000)
    host_allowlist: list[str] = Field(min_length=1, max_length=100)
    database_allowlist: list[str] = Field(min_length=1, max_length=100)
    catalog_allowlist: list[str] = Field(min_length=1, max_length=100)
    schema_allowlist: list[str] = Field(min_length=1, max_length=100)
    connect_timeout_sec: int = Field(default=10, ge=1, le=60)
    statement_timeout_ms: int = Field(default=15_000, ge=100, le=120_000)
    metadata_policy: JDBCMetadataPolicy = Field(default_factory=JDBCMetadataPolicy)

    @field_validator("connection_ref")
    @classmethod
    def validate_connection_ref(cls, value: str) -> str:
        if not _CREDENTIAL_REF.fullmatch(value):
            raise ValueError("connection_ref must be an opaque reference.")
        return value

    @field_validator("jdbc_url")
    @classmethod
    def reject_embedded_jdbc_secret(cls, value: str) -> str:
        raw = value.strip()
        parse_jdbc_url(raw)
        return raw

    @field_validator(
        "host_allowlist", "database_allowlist", "catalog_allowlist", "schema_allowlist"
    )
    @classmethod
    def normalize_jdbc_allowlist(cls, values: list[str], info) -> list[str]:
        normalized = []
        for raw in values:
            value = raw.strip().casefold() if info.field_name == "host_allowlist" else raw.strip()
            if (
                not value
                or len(value) > 255
                or any(character in value for character in ("/", "@", ":", "?", "#"))
            ):
                raise ValueError(f"{info.field_name} contains an invalid entry.")
            normalized.append(value.rstrip(".") if info.field_name == "host_allowlist" else value)
        return sorted(set(normalized))

    @model_validator(mode="after")
    def validate_managed_jdbc_target(self):
        if self.credential_ref is not None:
            raise ValueError("JDBC sources use connection_ref instead of credential_ref.")
        parsed = parse_jdbc_url(self.jdbc_url)
        if parsed.dialect != "postgresql":
            raise ValueError("Only the managed PostgreSQL JDBC driver is currently supported.")
        if self.driver_id != "postgresql":
            raise ValueError("driver_id is not present in the managed driver registry.")
        if parsed.host not in self.host_allowlist:
            raise ValueError("JDBC host must be explicitly listed in host_allowlist.")
        if not parsed.database or parsed.database not in self.database_allowlist:
            raise ValueError("JDBC database must be explicitly listed in database_allowlist.")
        if parsed.database not in self.catalog_allowlist:
            raise ValueError("PostgreSQL database must be listed in catalog_allowlist.")
        properties = {key.casefold(): value for key, value in parsed.properties.items()}
        if set(properties) - {"sslmode"}:
            raise ValueError("jdbc_url contains a property not allowed by the driver registry.")
        if properties.get("sslmode", "").casefold() != "verify-full":
            raise ValueError("PostgreSQL JDBC metadata connections require sslmode=verify-full.")
        return self


SourceConfig = Annotated[
    WebsiteSourceConfig | GitSourceConfig | JDBCSourceConfig,
    Field(discriminator="source_type"),
]
SOURCE_CONFIG_ADAPTER = TypeAdapter(SourceConfig)


def parse_source_config(value: dict[str, Any]) -> SourceConfig:
    return SOURCE_CONFIG_ADAPTER.validate_python(value)
