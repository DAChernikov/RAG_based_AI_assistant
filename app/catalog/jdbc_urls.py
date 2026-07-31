from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import parse_qsl, unquote, urlsplit


@dataclass(frozen=True)
class ParsedJDBCUrl:
    dialect: str
    host: str | None
    port: int | None
    database: str | None
    properties: dict[str, str]


def _secret_key(value: str) -> bool:
    normalized = re.sub(r"[^a-z0-9]", "", unquote(value).casefold())
    return normalized in {"user", "uid"} or any(
        marker in normalized
        for marker in (
            "password",
            "passwd",
            "pwd",
            "token",
            "secret",
            "apikey",
            "privatekey",
            "credential",
            "username",
        )
    )


def _reject_secret_properties(properties: dict[str, str]) -> None:
    if any(_secret_key(key) for key in properties):
        raise ValueError("jdbc_url must not contain credential properties.")


def parse_jdbc_url(value: str) -> ParsedJDBCUrl:
    raw = value.strip()
    lower = raw.casefold()
    if not lower.startswith("jdbc:"):
        raise ValueError("jdbc_url must start with jdbc:.")

    if lower.startswith("jdbc:postgresql://"):
        parsed = urlsplit(raw[5:])
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("jdbc_url must not contain embedded credentials.")
        properties = dict(parse_qsl(parsed.query, keep_blank_values=True))
        _reject_secret_properties(properties)
        database = unquote(parsed.path.lstrip("/")) or None
        return ParsedJDBCUrl(
            dialect="postgresql",
            host=(parsed.hostname or "").casefold() or None,
            port=parsed.port,
            database=database,
            properties=properties,
        )

    if lower.startswith("jdbc:sqlserver://"):
        endpoint, *raw_properties = raw[len("jdbc:sqlserver://") :].split(";")
        if "@" in endpoint:
            raise ValueError("jdbc_url must not contain embedded credentials.")
        parsed = urlsplit(f"sqlserver://{endpoint}")
        properties: dict[str, str] = {}
        for item in raw_properties:
            if not item:
                continue
            if "=" not in item:
                raise ValueError("SQL Server JDBC properties must use key=value syntax.")
            key, property_value = item.split("=", 1)
            properties[key.strip()] = property_value.strip()
        _reject_secret_properties(properties)
        database = next(
            (
                property_value
                for key, property_value in properties.items()
                if re.sub(r"[^a-z0-9]", "", key.casefold()) in {"database", "databasename"}
            ),
            None,
        )
        return ParsedJDBCUrl(
            dialect="sqlserver",
            host=(parsed.hostname or "").casefold() or None,
            port=parsed.port,
            database=database,
            properties=properties,
        )

    if lower.startswith("jdbc:oracle:thin:"):
        target = raw[len("jdbc:oracle:thin:") :]
        if not target.startswith("@"):
            raise ValueError("Oracle thin JDBC URL must not contain username/password.")
        target = target[1:]
        if re.search(
            r"(?i)(?:^|[?;&(),])\s*(?:password|passwd|pwd|token|secret|apikey)\s*=",
            target,
        ):
            raise ValueError("jdbc_url must not contain credential properties.")
        if target.startswith("//"):
            parsed = urlsplit(f"oracle:{target}")
            return ParsedJDBCUrl(
                dialect="oracle-thin",
                host=(parsed.hostname or "").casefold() or None,
                port=parsed.port,
                database=unquote(parsed.path.lstrip("/")) or None,
                properties={},
            )
        return ParsedJDBCUrl(
            dialect="oracle-thin",
            host=None,
            port=None,
            database=None,
            properties={},
        )

    parsed = urlsplit(raw[5:])
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("jdbc_url must not contain embedded credentials.")
    properties = dict(parse_qsl(parsed.query, keep_blank_values=True))
    _reject_secret_properties(properties)
    if re.search(
        r"(?i)(?:^|[?;&(),])\s*(?:password|passwd|pwd|token|secret|apikey)\s*=",
        raw,
    ):
        raise ValueError("jdbc_url must not contain credential properties.")
    return ParsedJDBCUrl(
        dialect=parsed.scheme.casefold(),
        host=(parsed.hostname or "").casefold() or None,
        port=parsed.port,
        database=unquote(parsed.path.lstrip("/")) or None,
        properties=properties,
    )
