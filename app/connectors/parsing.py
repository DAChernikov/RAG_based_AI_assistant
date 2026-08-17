from __future__ import annotations

import ast
import hashlib
import json
import re
from typing import Any

from app.connectors.base import ParsedChunk


def checksum_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _chunk(text: str, index: int, **metadata: Any) -> ParsedChunk:
    return ParsedChunk(
        text=text,
        checksum=checksum_text(text),
        chunk_index=index,
        metadata=metadata,
    )


def parse_markdown(text: str) -> tuple[ParsedChunk, ...]:
    lines = text.splitlines()
    chunks: list[ParsedChunk] = []
    start = 1
    heading = None
    buffer: list[str] = []
    for number, line in enumerate(lines, start=1):
        match = re.match(r"^(#{1,6})\s+(.+)$", line)
        if match and buffer:
            body = "\n".join(buffer).strip()
            if body:
                chunks.append(
                    _chunk(body, len(chunks), symbol=heading, line_start=start, line_end=number - 1)
                )
            buffer = []
        if match:
            heading = match.group(2).strip()
            start = number
        buffer.append(line)
    body = "\n".join(buffer).strip()
    if body:
        chunks.append(
            _chunk(body, len(chunks), symbol=heading, line_start=start, line_end=len(lines))
        )
    return tuple(chunks or [_chunk(text, 0, line_start=1, line_end=max(1, len(lines)))])


def parse_python(text: str) -> tuple[ParsedChunk, ...]:
    lines = text.splitlines()
    try:
        tree = ast.parse(text)
    except SyntaxError:
        return (_chunk(text, 0, line_start=1, line_end=max(1, len(lines))),)
    chunks: list[ParsedChunk] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            end = getattr(node, "end_lineno", node.lineno)
            body = "\n".join(lines[node.lineno - 1 : end])
            chunks.append(
                _chunk(
                    body,
                    len(chunks),
                    symbol=node.name,
                    symbol_type=node.__class__.__name__,
                    line_start=node.lineno,
                    line_end=end,
                )
            )
    return tuple(chunks or [_chunk(text, 0, line_start=1, line_end=max(1, len(lines)))])


def parse_sql(text: str) -> tuple[ParsedChunk, ...]:
    chunks: list[ParsedChunk] = []
    cursor = 1
    for statement in filter(None, (item.strip() for item in text.split(";"))):
        line_count = statement.count("\n") + 1
        symbol_match = re.search(
            r"(?i)\b(?:table|view|function|procedure)\s+([A-Za-z0-9_.\"]+)", statement
        )
        chunks.append(
            _chunk(
                statement + ";",
                len(chunks),
                symbol=symbol_match.group(1) if symbol_match else None,
                line_start=cursor,
                line_end=cursor + line_count - 1,
            )
        )
        cursor += line_count
    return tuple(chunks or [_chunk(text, 0, line_start=1, line_end=1)])


def parse_jvm(text: str) -> tuple[ParsedChunk, ...]:
    lines = text.splitlines()
    matches = list(
        re.finditer(
            r"(?m)^\s*(?:(?:public|private|protected|final|abstract|case)\s+)*(?:class|trait|object|interface|enum)\s+(\w+)",
            text,
        )
    )
    if not matches:
        return (_chunk(text, 0, line_start=1, line_end=max(1, len(lines))),)
    chunks: list[ParsedChunk] = []
    for index, match in enumerate(matches):
        start = text[: match.start()].count("\n") + 1
        end_offset = matches[index + 1].start() if index + 1 < len(matches) else len(text)
        end = text[:end_offset].count("\n") + 1
        chunks.append(
            _chunk(
                text[match.start() : end_offset].strip(),
                index,
                symbol=match.group(1),
                line_start=start,
                line_end=end,
            )
        )
    return tuple(chunks)


def parse_structured(text: str, language: str) -> tuple[ParsedChunk, ...]:
    if language == "json":
        try:
            payload = json.loads(text)
            if isinstance(payload, dict):
                return tuple(
                    _chunk(
                        json.dumps({key: value}, ensure_ascii=False, indent=2), index, symbol=key
                    )
                    for index, (key, value) in enumerate(payload.items())
                )
        except json.JSONDecodeError:
            pass
    lines = text.splitlines()
    chunks: list[ParsedChunk] = []
    for number, line in enumerate(lines, start=1):
        match = re.match(r"^([A-Za-z0-9_.-]+):\s*", line)
        if match:
            chunks.append(
                _chunk(line, len(chunks), symbol=match.group(1), line_start=number, line_end=number)
            )
    return tuple(chunks or [_chunk(text, 0, line_start=1, line_end=max(1, len(lines)))])


def parse_code(path: str, text: str) -> tuple[str, tuple[ParsedChunk, ...]]:
    suffix = path.rsplit(".", 1)[-1].lower() if "." in path else ""
    if suffix in {"md", "markdown"}:
        return "markdown", parse_markdown(text)
    if suffix == "py":
        return "python", parse_python(text)
    if suffix == "sql":
        return "sql", parse_sql(text)
    if suffix in {"scala", "java"}:
        return suffix, parse_jvm(text)
    if suffix in {"yaml", "yml", "json"}:
        language = "json" if suffix == "json" else "yaml"
        return language, parse_structured(text, language)
    lines = text.splitlines()
    return "text", (_chunk(text, 0, line_start=1, line_end=max(1, len(lines))),)
