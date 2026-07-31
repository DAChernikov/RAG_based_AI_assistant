from __future__ import annotations

import httpx
import pytest

from app.catalog.configs import WebsiteSourceConfig
from app.connectors.base import (
    CredentialMaterial,
    CredentialResolver,
    PreviousObject,
    SourceLimitError,
    SSRFProtectionError,
    TransientConnectorError,
)
from app.connectors.website import WebsiteConnector


class FakeCredentialResolver(CredentialResolver):
    async def resolve(self, reference: str) -> CredentialMaterial:
        return CredentialMaterial(http_headers={"Authorization": "Bearer test-only"})


async def allow_test_host(_host: str) -> None:
    return None


def config(**changes):
    values = {
        "source_type": "website",
        "root_url": "https://docs.example.test/",
        "allowed_domains": ["docs.example.test"],
        "max_pages": 10,
        "max_depth": 3,
        "max_page_bytes": 10_000,
        "requests_per_second": 100,
        "max_retries": 1,
        "use_sitemap": False,
    }
    values.update(changes)
    return WebsiteSourceConfig(**values)


@pytest.mark.asyncio
async def test_website_discovery_extracts_structure_and_incremental_changes():
    first_pages = {
        "/": (
            200,
            '<html><head><title>Docs</title><meta name="description" content="Home"></head>'
            '<body><h1>Start</h1><p>Hello</p><a href="/guide">Guide</a></body></html>',
            {"etag": '"root-v1"', "content-type": "text/html"},
        ),
        "/guide": (
            200,
            "<h1>Guide</h1><ul><li>Step one</li></ul><pre>print(1)</pre>",
            {"etag": '"guide-v1"', "content-type": "text/html"},
        ),
    }

    def first_handler(request: httpx.Request):
        status, body, headers = first_pages[request.url.path]
        return httpx.Response(status, text=body, headers=headers)

    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(first_handler),
        host_validator=allow_test_host,
    )
    first = await connector.discover(config(credential_ref="credential:docs"))
    assert first.added == (
        "https://docs.example.test/",
        "https://docs.example.test/guide",
    )
    guide = next(item for item in first.documents if item.object_key.endswith("/guide"))
    assert {chunk.metadata["element"] for chunk in guide.chunks} >= {"h1", "li", "pre"}

    previous = {
        item.object_key: PreviousObject(item.object_key, item.checksum, item.metadata)
        for item in first.documents
    }

    def second_handler(request: httpx.Request):
        assert request.headers.get("if-none-match")
        if request.url.path == "/":
            return httpx.Response(304)
        return httpx.Response(
            200,
            text="<h1>Guide</h1><p>Changed</p>",
            headers={"etag": '"guide-v2"', "content-type": "text/html"},
        )

    second = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(second_handler),
        host_validator=allow_test_host,
    )
    incremental = await second.discover(config(credential_ref="credential:docs"), previous)
    assert incremental.unchanged == ("https://docs.example.test/",)
    assert incremental.modified == ("https://docs.example.test/guide",)

    changed_guide = incremental.documents[0]
    latest = {
        "https://docs.example.test/": previous["https://docs.example.test/"],
        changed_guide.object_key: PreviousObject(
            changed_guide.object_key, changed_guide.checksum, changed_guide.metadata
        ),
    }
    third = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(304 if request.url.path == "/" else 404)
        ),
        host_validator=allow_test_host,
    )
    deleted = await third.discover(config(credential_ref="credential:docs"), latest)
    assert deleted.deleted == ("https://docs.example.test/guide",)


@pytest.mark.asyncio
async def test_website_redirect_is_revalidated_against_allowlist():
    def handler(_request: httpx.Request):
        return httpx.Response(302, headers={"location": "http://169.254.169.254/latest/meta-data"})

    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(handler),
        host_validator=allow_test_host,
    )
    with pytest.raises(SSRFProtectionError):
        await connector.discover(config())

    checked = []

    async def reject_redirect_host(host: str):
        checked.append(host)
        if host.startswith("internal."):
            raise SSRFProtectionError("resolved to private address")

    same_allowlist = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                302, headers={"location": "https://internal.docs.example.test/secret"}
            )
        ),
        host_validator=reject_redirect_host,
    )
    with pytest.raises(SSRFProtectionError, match="private"):
        await same_allowlist.discover(config())
    assert checked == ["docs.example.test", "internal.docs.example.test"]


@pytest.mark.asyncio
async def test_website_retries_temporary_response_without_leaking_credentials():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(503)
        return httpx.Response(200, text="<p>Ready</p>", headers={"content-type": "text/html"})

    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(handler),
        host_validator=allow_test_host,
    )
    result = await connector.discover(config(credential_ref="credential:docs"))
    assert len(result.documents) == 1
    assert requests[-1].headers["authorization"] == "Bearer test-only"

    always_failing = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(lambda _request: httpx.Response(503)),
        host_validator=allow_test_host,
    )
    with pytest.raises(TransientConnectorError, match="temporary error"):
        await always_failing.discover(config(max_retries=0))


@pytest.mark.asyncio
async def test_website_enforces_page_size_limit():
    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=b"x" * 2048,
                headers={"content-type": "text/html"},
            )
        ),
        host_validator=allow_test_host,
    )
    with pytest.raises(SourceLimitError, match="page size"):
        await connector.discover(config(max_page_bytes=1024))
