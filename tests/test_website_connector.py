from __future__ import annotations

from types import SimpleNamespace

import httpx
import pytest

from app.catalog.configs import WebsiteSourceConfig
from app.connectors.base import (
    CredentialIsolationError,
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


@pytest.mark.asyncio
async def test_truncated_crawl_preserves_unvisited_previous_objects():
    previous = {
        f"https://docs.example.test/page-{index}": PreviousObject(
            f"https://docs.example.test/page-{index}", "a" * 64, {"etag": '"old"'}
        )
        for index in range(3)
    }
    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(
            lambda request: httpx.Response(
                200,
                text="<p>Root</p>",
                headers={"content-type": "text/html"},
                request=request,
            )
        ),
        host_validator=allow_test_host,
    )
    result = await connector.discover(
        config(max_pages=1, credential_ref="credential:docs"), previous
    )
    assert result.complete is False
    assert result.incomplete_reasons == ("max_pages",)
    assert result.deleted == ()
    assert set(result.unchanged) == set(previous)


@pytest.mark.asyncio
async def test_depth_limited_crawl_cannot_delete_previously_known_page():
    previous_url = "https://docs.example.test/previous"
    previous = {previous_url: PreviousObject(previous_url, "a" * 64, {"etag": '"old"'})}

    def handler(request: httpx.Request):
        if request.url.path == "/":
            return httpx.Response(
                200,
                text='<a href="/too-deep">Deep</a>',
                headers={"content-type": "text/html"},
            )
        return httpx.Response(404)

    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(handler),
        host_validator=allow_test_host,
    )
    result = await connector.discover(
        config(max_depth=0, use_sitemap=False, credential_ref="credential:docs"), previous
    )
    assert result.complete is False
    assert result.incomplete_reasons == ("max_depth",)
    assert result.deleted == ()
    assert result.unchanged == (previous_url,)


@pytest.mark.asyncio
async def test_time_limited_crawl_preserves_previous_objects(monkeypatch):
    previous_url = "https://docs.example.test/previous"
    previous = {previous_url: PreviousObject(previous_url, "a" * 64)}
    ticks = iter((0.0, 10.0))
    monkeypatch.setattr(
        "app.connectors.website.time", SimpleNamespace(monotonic=lambda: next(ticks))
    )
    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(lambda _request: pytest.fail("request was not expected")),
        host_validator=allow_test_host,
    )
    result = await connector.discover(config(max_crawl_seconds=1, use_sitemap=False), previous)
    assert result.complete is False
    assert result.incomplete_reasons == ("max_crawl_seconds",)
    assert result.deleted == ()
    assert result.unchanged == (previous_url,)


@pytest.mark.asyncio
async def test_tree_parser_preserves_nested_text_and_table_structure():
    html = (
        "<h1>API</h1><p>Hello <strong>nested <code>call()</code></strong> tail.</p>"
        "<ul><li>First <em>item</em></li></ul>"
        "<pre>line 1\nline 2</pre>"
        "<table><tr><th>Name</th><th>Type</th></tr>"
        "<tr><td>id</td><td><code>uuid</code></td></tr></table>"
    )
    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(200, text=html, headers={"content-type": "text/html"})
        ),
        host_validator=allow_test_host,
    )
    result = await connector.discover(config())
    chunks = result.documents[0].chunks
    paragraph = next(item for item in chunks if item.metadata["element"] == "p")
    assert paragraph.text == "Hello nested call() tail."
    assert paragraph.metadata["headings"] == ["API"]
    assert any(item.text == "First item" for item in chunks)
    rows = [item for item in chunks if item.metadata["element"] == "tr"]
    assert rows[0].text == "Name | Type"
    assert rows[1].metadata["cells"][1]["text"] == "uuid"


@pytest.mark.asyncio
async def test_website_credentials_are_origin_scoped_and_headers_allowlisted():
    requests = []

    def handler(request: httpx.Request):
        requests.append(request)
        if request.url.host == "docs.example.test":
            return httpx.Response(302, headers={"location": "https://cdn.example.test/page"})
        return httpx.Response(200, text="<p>ok</p>", headers={"content-type": "text/html"})

    connector = WebsiteConnector(
        FakeCredentialResolver(),
        transport=httpx.MockTransport(handler),
        host_validator=allow_test_host,
    )
    await connector.discover(
        config(
            allowed_domains=["example.test"],
            credential_ref="credential:docs",
        )
    )
    assert requests[0].headers.get("authorization") == "Bearer test-only"
    assert "authorization" not in requests[1].headers

    class UnsafeResolver(CredentialResolver):
        async def resolve(self, reference: str) -> CredentialMaterial:
            return CredentialMaterial(http_headers={"Host": "internal.example.test"})

    unsafe = WebsiteConnector(
        UnsafeResolver(),
        transport=httpx.MockTransport(handler),
        host_validator=allow_test_host,
    )
    with pytest.raises(CredentialIsolationError):
        await unsafe.discover(config(credential_ref="credential:docs"))


@pytest.mark.asyncio
async def test_website_rejects_cross_connector_credential_material():
    class CrossChannelResolver(CredentialResolver):
        async def resolve(self, _reference: str) -> CredentialMaterial:
            return CredentialMaterial(
                database_parameters={"username": "wrong-channel", "password": "fixture"}
            )

    connector = WebsiteConnector(
        CrossChannelResolver(),
        transport=httpx.MockTransport(lambda _request: pytest.fail("request was not expected")),
        host_validator=allow_test_host,
    )
    with pytest.raises(CredentialIsolationError, match="non-HTTP"):
        await connector.discover(config(credential_ref="credential:unsafe"))
