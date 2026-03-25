import json
from typing import AsyncIterator

import httpx

from app.api.config import settings


class LLMService:
    def __init__(self):
        self.provider = settings.llm_provider
        self.model = settings.llm_model
        self.api_key = settings.llm_api_key
        self.timeout = settings.request_timeout

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

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:generateContent?key={self.api_key}"
        )

        body = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "You are a helpful RAG assistant. "
                            "Answer strictly from the provided context. "
                            "If the answer is not grounded in the context, say so explicitly. "
                            "Be concise and useful."
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

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(url, json=body)
            response.raise_for_status()
            payload = response.json()

        return self._extract_text_from_gemini_payload(payload)

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

        url = (
            f"https://generativelanguage.googleapis.com/v1beta/models/"
            f"{self.model}:streamGenerateContent?alt=sse&key={self.api_key}"
        )

        body = {
            "systemInstruction": {
                "parts": [
                    {
                        "text": (
                            "You are a helpful RAG assistant. "
                            "Answer strictly from the provided context. "
                            "If the answer is not grounded in the context, say so explicitly. "
                            "Be concise and useful."
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

        async with httpx.AsyncClient(timeout=None) as client:
            async with client.stream("POST", url, json=body) as response:
                response.raise_for_status()

                async for line in response.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue

                    raw = line[len("data:") :].strip()
                    if not raw:
                        continue

                    try:
                        payload = json.loads(raw)
                    except json.JSONDecodeError:
                        continue

                    chunk = self._extract_text_from_gemini_payload(payload)
                    if chunk:
                        yield chunk
