from __future__ import annotations

import asyncio
import json
from typing import AsyncIterator

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


class LLMService:
    """Асинхронный клиент для Gemini модели"""

    RETRYABLE_STATUSES = {429, 500, 502, 503, 504}
    TEMPORARY_UNAVAILABLE_STATUSES = {500, 502, 503, 504}

    def __init__(self):
        self.provider = settings.llm_provider
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.timeout = settings.request_timeout
        self.api_base_url = settings.llm_api_base_url.rstrip("/")

    def is_configured(self) -> bool:
        return bool(self.api_key and self.provider == "gemini")

    @staticmethod
    def _extract_text_from_gemini_payload(payload: dict) -> str:
        texts: list[str] = []
        for candidate in payload.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                text = part.get("text")
                if text:
                    texts.append(text)
        return "".join(texts).strip()

    @staticmethod
    def _extract_delta(previous_text: str, current_text: str) -> str:
        if not current_text:
            return ""

        if current_text.startswith(previous_text):
            return current_text[len(previous_text) :]

        if previous_text.startswith(current_text):
            return ""

        common_len = 0
        for prev_char, curr_char in zip(previous_text, current_text):
            if prev_char != curr_char:
                break
            common_len += 1

        delta = current_text[common_len:]
        return delta if delta else current_text

    def _build_body(self, prompt: str, max_new_tokens: int, temperature: float) -> dict:
        return {
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "You are a reliable RAG assistant. "
                            "Stay grounded in the supplied context. "
                            "Be concise, useful, and technical."
                        )
                    }
                ]
            },
            "contents": [{"role": "user", "parts": [{"text": prompt}]}],
            "generationConfig": {
                "temperature": temperature,
                "maxOutputTokens": max_new_tokens,
            },
        }

    def _build_url(self, method: str) -> str:
        return f"{self.api_base_url}/models/{self.model}:{method}?key={self.api_key}"

    @classmethod
    def _raise_for_llm_status(cls, response: httpx.Response) -> None:
        if response.status_code == 429:
            raise LLMRateLimitError()

        if response.status_code in cls.TEMPORARY_UNAVAILABLE_STATUSES:
            raise LLMTemporaryUnavailableError()

        response.raise_for_status()

    async def generate(
        self,
        *,
        prompt: str,
        max_new_tokens: int,
        temperature: float | None = None,
    ) -> str:
        if not self.is_configured():
            raise RuntimeError("LLM is not configured.")

        temperature = settings.llm_temperature if temperature is None else temperature
        url = self._build_url("generateContent")
        body = self._build_body(prompt, max_new_tokens, temperature)

        last_error: Exception | None = None
        last_status_code: int | None = None

        for attempt in range(settings.llm_retries + 1):
            try:
                async with httpx.AsyncClient(timeout=self.timeout) as client:
                    response = await client.post(url, json=body)

                last_status_code = response.status_code

                if (
                    response.status_code in self.RETRYABLE_STATUSES
                    and attempt < settings.llm_retries
                ):
                    await asyncio.sleep(settings.llm_retry_backoff_sec * (attempt + 1))
                    continue

                self._raise_for_llm_status(response)
                payload = response.json()
                return self._extract_text_from_gemini_payload(payload)

            except (LLMRateLimitError, LLMTemporaryUnavailableError):
                raise

            except (httpx.HTTPError, httpx.TimeoutException) as exc:
                last_error = exc
                if attempt >= settings.llm_retries:
                    break
                await asyncio.sleep(settings.llm_retry_backoff_sec * (attempt + 1))

        if last_status_code == 429:
            raise LLMRateLimitError()

        if last_status_code in self.TEMPORARY_UNAVAILABLE_STATUSES:
            raise LLMTemporaryUnavailableError()

        if last_error:
            raise RuntimeError(f"LLM request failed: {last_error}") from last_error

        return ""

    async def stream_generate(
        self,
        *,
        prompt: str,
        max_new_tokens: int,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        if not self.is_configured():
            raise RuntimeError("LLM is not configured.")

        temperature = settings.llm_temperature if temperature is None else temperature
        url = self._build_url("streamGenerateContent") + "&alt=sse"
        body = self._build_body(prompt, max_new_tokens, temperature)

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=body) as response:
                self._raise_for_llm_status(response)

                emitted_text = ""

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue

                    raw = line[len("data:") :].strip()
                    if not raw or raw == "[DONE]":
                        continue

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    current_text = self._extract_text_from_gemini_payload(payload)
                    delta = self._extract_delta(emitted_text, current_text)
                    if delta:
                        emitted_text += delta
                        yield delta
