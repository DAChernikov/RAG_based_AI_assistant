from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator

import httpx

from app.api.config import settings


class LLMRateLimitError(RuntimeError):
    def __init__(
        self,
        message: str = (
            "Сервис временно упёрся в лимит LLM API. "
            "Попробуйте повторить запрос через некоторое время."
        ),
    ):
        super().__init__(message)


class LLMTemporaryUnavailableError(RuntimeError):
    def __init__(
        self,
        message: str = (
            "Сервис LLM временно недоступен. " "Попробуйте повторить запрос через минуту."
        ),
    ):
        super().__init__(message)


class OpenAICompatibleLLMService:
    """Async client for a self-hosted OpenAI-compatible chat completions API."""

    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
    TEMPORARY_UNAVAILABLE_STATUSES = {500, 502, 503, 504}
    SYSTEM_PROMPT = (
        "You are a reliable RAG assistant. "
        "Stay grounded in the supplied context. "
        "Be concise, useful, and technical."
    )

    def __init__(
        self,
        *,
        api_base_url: str | None = None,
        model: str | None = None,
        api_token: str | None = None,
        timeout: float | None = None,
        retries: int | None = None,
        retry_backoff_sec: float | None = None,
        default_temperature: float | None = None,
        client: httpx.AsyncClient | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
    ):
        if client is not None and transport is not None:
            raise ValueError("Pass either client or transport, not both.")

        self.model = model if model is not None else settings.generation_model
        configured_token = settings.model_api_token if api_token is None else api_token
        self.api_token = configured_token.strip() if configured_token else None
        self.timeout = timeout if timeout is not None else settings.model_request_timeout
        self.retries = retries if retries is not None else settings.model_retries
        self.retry_backoff_sec = (
            retry_backoff_sec if retry_backoff_sec is not None else settings.model_retry_backoff_sec
        )
        self.default_temperature = (
            default_temperature if default_temperature is not None else settings.model_temperature
        )
        configured_base_url = (
            api_base_url if api_base_url is not None else settings.model_api_base_url
        )
        self.api_base_url = configured_base_url.rstrip("/")
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=self.timeout, transport=transport)

    def is_configured(self) -> bool:
        return bool(self.api_base_url and self.model)

    @property
    def endpoint(self) -> str:
        return f"{self.api_base_url}/chat/completions"

    def _headers(self, *, streaming: bool = False) -> dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if streaming:
            headers["Accept"] = "text/event-stream"
        if self.api_token:
            headers["Authorization"] = f"Bearer {self.api_token}"
        return headers

    def _build_body(
        self,
        prompt: str,
        max_new_tokens: int,
        temperature: float,
        *,
        stream: bool,
    ) -> dict:
        return {
            "model": self.model,
            "messages": [
                {"role": "system", "content": self.SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            "temperature": temperature,
            "max_tokens": max_new_tokens,
            "stream": stream,
        }

    @classmethod
    def _raise_for_llm_status(cls, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise LLMRateLimitError()

        if response.status_code in cls.TEMPORARY_UNAVAILABLE_STATUSES:
            raise LLMTemporaryUnavailableError()

        response.raise_for_status()

    async def _backoff(self, attempt: int) -> None:
        delay = self.retry_backoff_sec * (attempt + 1)
        if delay > 0:
            await asyncio.sleep(delay)

    @staticmethod
    def _extract_message_content(payload: dict) -> str:
        try:
            content = payload["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError) as exc:
            raise RuntimeError(
                "Model response does not contain choices[0].message.content."
            ) from exc

        if not isinstance(content, str):
            raise RuntimeError("Model response content must be a string.")
        return content

    @staticmethod
    def _extract_stream_delta(payload: dict) -> str:
        try:
            content = payload["choices"][0]["delta"].get("content")
        except (KeyError, IndexError, TypeError, AttributeError):
            return ""
        return content if isinstance(content, str) else ""

    async def generate(
        self,
        *,
        prompt: str,
        max_new_tokens: int,
        temperature: float | None = None,
    ) -> str:
        if not self.is_configured():
            raise RuntimeError("LLM is not configured.")

        effective_temperature = self.default_temperature if temperature is None else temperature
        body = self._build_body(
            prompt,
            max_new_tokens,
            effective_temperature,
            stream=False,
        )

        for attempt in range(self.retries + 1):
            try:
                response = await self._client.post(
                    self.endpoint,
                    json=body,
                    headers=self._headers(),
                    timeout=self.timeout,
                )
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if attempt < self.retries:
                    await self._backoff(attempt)
                    continue
                raise LLMTemporaryUnavailableError() from exc

            if response.status_code in self.RETRYABLE_STATUSES and attempt < self.retries:
                await self._backoff(attempt)
                continue

            self._raise_for_llm_status(response)
            return self._extract_message_content(response.json())

    async def stream_generate(
        self,
        *,
        prompt: str,
        max_new_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        if not self.is_configured():
            raise RuntimeError("LLM is not configured.")

        effective_temperature = self.default_temperature if temperature is None else temperature
        body = self._build_body(
            prompt,
            max_new_tokens,
            effective_temperature,
            stream=True,
        )

        for attempt in range(self.retries + 1):
            emitted = False
            should_retry_status = False

            try:
                async with self._client.stream(
                    "POST",
                    self.endpoint,
                    json=body,
                    headers=self._headers(streaming=True),
                    timeout=self.timeout,
                ) as response:
                    if response.status_code in self.RETRYABLE_STATUSES and attempt < self.retries:
                        should_retry_status = True
                    else:
                        self._raise_for_llm_status(response)

                        async for line in response.aiter_lines():
                            if not line or not line.startswith("data:"):
                                continue

                            raw = line[len("data:") :].strip()
                            if not raw:
                                continue
                            if raw == "[DONE]":
                                return

                            try:
                                payload = json.loads(raw)
                            except json.JSONDecodeError:
                                continue

                            delta = self._extract_stream_delta(payload)
                            if delta:
                                emitted = True
                                yield delta

                        return
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                if emitted or attempt >= self.retries:
                    raise LLMTemporaryUnavailableError() from exc
                await self._backoff(attempt)
                continue

            if should_retry_status:
                await self._backoff(attempt)
                continue

    async def aclose(self) -> None:
        if self._owns_client and not self._client.is_closed:
            await self._client.aclose()

    async def __aenter__(self) -> OpenAICompatibleLLMService:
        return self

    async def __aexit__(self, exc_type, exc, traceback) -> None:
        await self.aclose()


# Temporary compatibility alias used by RAGService, SQLService, and existing imports.
LLMService = OpenAICompatibleLLMService
