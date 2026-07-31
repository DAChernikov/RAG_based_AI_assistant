from __future__ import annotations

import asyncio
import fnmatch
import hashlib
import ipaddress
import socket
import time
from collections import deque
from dataclasses import dataclass, field
from html.parser import HTMLParser
from typing import Awaitable, Callable
from urllib.parse import urldefrag, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree

import httpx

from app.catalog.configs import WebsiteSourceConfig
from app.connectors.base import (
    ConnectorDocument,
    CredentialIsolationError,
    CredentialMaterial,
    CredentialResolver,
    DiscoveryResult,
    ParsedChunk,
    PreviousObject,
    SourceLimitError,
    SSRFProtectionError,
    TransientConnectorError,
    validate_credential_material,
)
from app.connectors.parsing import checksum_text

_BLOCK_TAGS = {
    "p",
    "li",
    "pre",
    "code",
    "td",
    "th",
    "tr",
    "blockquote",
    *(f"h{i}" for i in range(1, 7)),
}
_BINARY_CONTENT_PREFIXES = ("image/", "audio/", "video/", "application/octet-stream")


@dataclass
class _OpenElement:
    tag: str
    parts: list[str] = field(default_factory=list)
    cells: list[dict[str, str | int]] = field(default_factory=list)


class _HTMLExtractor(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.title = ""
        self.links: list[str] = []
        self.canonical: str | None = None
        self.metadata: dict[str, str] = {}
        self.blocks: list[dict] = []
        self.headings: list[str] = []
        self._elements: list[_OpenElement] = []
        self._tag_stack: list[str] = []
        self._ignored_depth = 0

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        attributes = dict(attrs)
        if tag == "a" and attributes.get("href"):
            self.links.append(attributes["href"])
        if tag == "link" and attributes.get("rel", "").lower() == "canonical":
            self.canonical = attributes.get("href")
        if tag == "meta":
            key = attributes.get("name") or attributes.get("property")
            if key and attributes.get("content"):
                self.metadata[key[:100]] = attributes["content"][:1000]
        self._tag_stack.append(tag)
        if tag in {"script", "style", "noscript"}:
            self._ignored_depth += 1
        if tag == "title" or tag in _BLOCK_TAGS:
            self._elements.append(_OpenElement(tag=tag))

    def handle_data(self, data):
        if self._ignored_depth:
            return
        for element in self._elements:
            element.parts.append(data)

    def handle_endtag(self, tag):
        tag = tag.lower()
        matching = next(
            (
                index
                for index in range(len(self._elements) - 1, -1, -1)
                if self._elements[index].tag == tag
            ),
            None,
        )
        if matching is not None:
            while len(self._elements) > matching:
                self._finish(self._elements.pop())
        if tag in {"script", "style", "noscript"} and self._ignored_depth:
            self._ignored_depth -= 1
        if tag in self._tag_stack:
            reverse_index = self._tag_stack[::-1].index(tag)
            del self._tag_stack[len(self._tag_stack) - reverse_index - 1 :]

    def close(self):
        while self._elements:
            self._finish(self._elements.pop())
        super().close()

    def _finish(self, element: _OpenElement) -> None:
        raw = "".join(element.parts)
        text = raw.strip() if element.tag in {"pre", "code"} else " ".join(raw.split())
        if not text:
            return
        if element.tag == "title":
            self.title = text
            return
        if element.tag.startswith("h") and len(element.tag) == 2:
            level = int(element.tag[1])
            self.headings = self.headings[: level - 1]
            self.headings.append(text)
        metadata: dict[str, object] = {}
        if element.tag in {"td", "th"}:
            row = next((item for item in reversed(self._elements) if item.tag == "tr"), None)
            if row is not None:
                row.cells.append({"kind": element.tag, "text": text, "column": len(row.cells)})
            metadata["column"] = len(row.cells) - 1 if row is not None else 0
        if element.tag == "tr":
            metadata["cells"] = list(element.cells)
            if element.cells:
                text = " | ".join(str(cell["text"]) for cell in element.cells)
        if element.tag == "li":
            metadata["list_depth"] = sum(item in {"ul", "ol"} for item in self._tag_stack)
        self.blocks.append(
            {
                "element": element.tag,
                "text": text,
                "headings": tuple(self.headings),
                "metadata": metadata,
            }
        )


async def _resolve_public(host: str) -> None:
    rows = await asyncio.to_thread(socket.getaddrinfo, host, None, type=socket.SOCK_STREAM)
    if not rows:
        raise SSRFProtectionError("Host did not resolve.")
    for row in rows:
        address = ipaddress.ip_address(row[4][0])
        if not address.is_global:
            raise SSRFProtectionError("Private, link-local and metadata addresses are blocked.")


class WebsiteConnector:
    connector_version = "website/1.0"

    def __init__(
        self,
        credential_resolver: CredentialResolver,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        host_validator: Callable[[str], Awaitable[None]] = _resolve_public,
    ):
        self.credential_resolver = credential_resolver
        self.transport = transport
        self.host_validator = host_validator
        self._last_request_at = 0.0

    @staticmethod
    def _normalize_url(value: str) -> str:
        clean, _ = urldefrag(value)
        parsed = urlsplit(clean)
        path = parsed.path or "/"
        return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(), path, parsed.query, ""))

    @staticmethod
    def _origin(value: str) -> str:
        parsed = urlsplit(value)
        host = (parsed.hostname or "").lower().rstrip(".")
        default_port = 443 if parsed.scheme == "https" else 80
        port = f":{parsed.port}" if parsed.port and parsed.port != default_port else ""
        return f"{parsed.scheme.lower()}://{host}{port}"

    @staticmethod
    def _allowed(url: str, config: WebsiteSourceConfig) -> bool:
        parsed = urlsplit(url)
        host = (parsed.hostname or "").lower().rstrip(".")
        if parsed.scheme not in {"http", "https"}:
            return False
        if not any(
            host == domain or host.endswith(f".{domain}") for domain in config.allowed_domains
        ):
            return False
        candidate = parsed.path or "/"
        if config.include_patterns and not any(
            fnmatch.fnmatch(candidate, item) for item in config.include_patterns
        ):
            return False
        return not any(fnmatch.fnmatch(candidate, item) for item in config.exclude_patterns)

    async def _request(
        self,
        client: httpx.AsyncClient,
        url: str,
        config: WebsiteSourceConfig,
        request_headers: dict[str, str],
        credential_headers: dict[str, str],
        credential_origins: frozenset[str],
    ) -> httpx.Response:
        current = self._normalize_url(url)
        for _redirect in range(6):
            parsed = urlsplit(current)
            await self.host_validator(parsed.hostname or "")
            wait = max(
                0.0, 1.0 / config.requests_per_second - (time.monotonic() - self._last_request_at)
            )
            if wait:
                await asyncio.sleep(wait)
            response = None
            for attempt in range(config.max_retries + 1):
                try:
                    headers = dict(request_headers)
                    if self._origin(current) in credential_origins:
                        headers.update(credential_headers)
                    response = await client.get(current, headers=headers)
                except (httpx.TimeoutException, httpx.TransportError) as exc:
                    if attempt >= config.max_retries:
                        raise TransientConnectorError(
                            "Website request failed after retries."
                        ) from exc
                    await asyncio.sleep(min(2**attempt, 5))
                    continue
                self._last_request_at = time.monotonic()
                if response.status_code == 429 or 500 <= response.status_code < 600:
                    if attempt >= config.max_retries:
                        raise TransientConnectorError("Website returned a temporary error.")
                    await asyncio.sleep(min(2**attempt, 5))
                    continue
                break
            if response is None:
                raise TransientConnectorError("Website request did not produce a response.")
            if response.status_code in {301, 302, 303, 307, 308}:
                location = response.headers.get("location")
                if not location:
                    raise TransientConnectorError("Website redirect had no location.")
                target = self._normalize_url(urljoin(current, location))
                if not self._allowed(target, config):
                    raise SSRFProtectionError("Redirect left the configured allowlist.")
                current = target
                continue
            return response
        raise SSRFProtectionError("Too many redirects.")

    async def discover(
        self,
        config: WebsiteSourceConfig,
        previous: dict[str, PreviousObject] | None = None,
    ) -> DiscoveryResult:
        previous = previous or {}
        credentials = validate_credential_material(
            await self.credential_resolver.resolve(config.credential_ref)
            if config.credential_ref
            else CredentialMaterial()
        )
        if credentials.git_environment or credentials.database_parameters:
            raise CredentialIsolationError(
                "Website credential reference contains non-HTTP credential material."
            )
        credential_origins = frozenset(
            {self._origin(str(config.root_url)), *config.credential_allowed_origins}
            if config.credential_ref
            else set()
        )
        queue: deque[tuple[str, int, bool]] = deque(
            [(self._normalize_url(str(config.root_url)), 0, False)]
        )
        if config.use_sitemap:
            queue.append((urljoin(str(config.root_url), "/sitemap.xml"), 0, True))
        for known_url in sorted(previous):
            queue.append((known_url, 0, False))
        visited: set[str] = set()
        confirmed: set[str] = set()
        documents: list[ConnectorDocument] = []
        started_at = time.monotonic()
        complete = True
        incomplete_reasons: set[str] = set()
        async with httpx.AsyncClient(
            transport=self.transport,
            timeout=config.request_timeout_sec,
            follow_redirects=False,
        ) as client:
            while queue:
                if len(visited) >= config.max_pages:
                    complete = False
                    incomplete_reasons.add("max_pages")
                    break
                if time.monotonic() - started_at > config.max_crawl_seconds:
                    complete = False
                    incomplete_reasons.add("max_crawl_seconds")
                    break
                url, depth, is_sitemap = queue.popleft()
                url = self._normalize_url(url)
                if url in visited or (not is_sitemap and not self._allowed(url, config)):
                    continue
                if depth > config.max_depth:
                    complete = False
                    incomplete_reasons.add("max_depth")
                    continue
                visited.add(url)
                old = previous.get(url)
                headers = {}
                if old:
                    if old.metadata.get("etag"):
                        headers["If-None-Match"] = old.metadata["etag"]
                    if old.metadata.get("last_modified"):
                        headers["If-Modified-Since"] = old.metadata["last_modified"]
                response = await self._request(
                    client,
                    url,
                    config,
                    headers,
                    credentials.http_headers,
                    credential_origins,
                )
                if response.status_code == 304 and old:
                    confirmed.add(old.object_key)
                    continue
                if response.status_code == 404:
                    continue
                response.raise_for_status()
                content_type = response.headers.get("content-type", "").split(";", 1)[0].lower()
                if content_type.startswith(_BINARY_CONTENT_PREFIXES):
                    continue
                body = response.content
                if len(body) > config.max_page_bytes:
                    raise SourceLimitError("Website page size limit exceeded.")
                if content_type in {"application/xml", "text/xml"} or url.endswith("sitemap.xml"):
                    try:
                        root = ElementTree.fromstring(body)
                    except ElementTree.ParseError:
                        continue
                    for element in root.iter():
                        if element.tag.rsplit("}", 1)[-1] == "loc" and element.text:
                            target = self._normalize_url(element.text.strip())
                            if self._allowed(target, config):
                                queue.append((target, 0, False))
                    continue
                if content_type not in {"text/html", "application/xhtml+xml", ""}:
                    continue
                extractor = _HTMLExtractor()
                extractor.feed(response.text)
                extractor.close()
                canonical = self._normalize_url(urljoin(url, extractor.canonical or url))
                if not self._allowed(canonical, config):
                    canonical = url
                chunks = tuple(
                    ParsedChunk(
                        text=block["text"],
                        checksum=checksum_text(text),
                        chunk_index=index,
                        metadata={
                            "element": block["element"],
                            "headings": list(block["headings"]),
                            **block["metadata"],
                        },
                    )
                    for index, block in enumerate(extractor.blocks)
                    for text in [block["text"]]
                )
                text = "\n\n".join(chunk.text for chunk in chunks)
                checksum = hashlib.sha256(body).hexdigest()
                documents.append(
                    ConnectorDocument(
                        object_key=canonical,
                        canonical_uri=canonical,
                        title=extractor.title or canonical,
                        text=text,
                        checksum=checksum,
                        byte_count=len(body),
                        metadata={
                            "etag": response.headers.get("etag"),
                            "last_modified": response.headers.get("last-modified"),
                            "html_metadata": extractor.metadata,
                            "headings": [
                                list(item["headings"])
                                for item in extractor.blocks
                                if item["element"].startswith("h")
                            ],
                        },
                        chunks=chunks,
                    )
                )
                confirmed.add(canonical)
                for link in extractor.links:
                    target = self._normalize_url(urljoin(url, link))
                    if self._allowed(target, config):
                        queue.append((target, depth + 1, False))

        by_key = {document.object_key: document for document in documents}
        # A conditional 304 represents an unchanged object not materialized by this adapter.
        unchanged = {key for key in previous if key in confirmed and key not in by_key}
        unchanged.update(
            key
            for key, doc in by_key.items()
            if previous.get(key) and previous[key].checksum == doc.checksum
        )
        added = tuple(sorted(set(by_key) - set(previous)))
        modified = tuple(sorted(key for key in by_key if key in previous and key not in unchanged))
        if complete:
            deleted = tuple(sorted(set(previous) - confirmed - set(by_key)))
        else:
            deleted = ()
            unchanged.update(set(previous) - set(by_key))
        return DiscoveryResult(
            documents=tuple(by_key[key] for key in sorted(by_key)),
            added=added,
            modified=modified,
            unchanged=tuple(sorted(unchanged)),
            deleted=deleted,
            complete=complete,
            incomplete_reasons=tuple(sorted(incomplete_reasons)),
        )
