import json

import httpx
import pytest

from app.api.services.llm_service import (
    LLMModelUnavailableError,
    LLMRateLimitError,
    LLMTemporaryUnavailableError,
    OpenAICompatibleLLMService,
)


def _success_response(content: str = "Grounded answer") -> dict:
    return {"choices": [{"message": {"content": content}}]}


@pytest.mark.asyncio
async def test_generate_uses_openai_compatible_payload_without_api_token():
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_success_response())

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        api_token="",
        retries=0,
        default_temperature=0.25,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        result = await service.generate(prompt="Question and context", max_new_tokens=321)

    assert result == "Grounded answer"
    assert len(requests) == 1
    request = requests[0]
    assert request.url == "http://model.test/v1/chat/completions"
    assert "authorization" not in request.headers

    payload = json.loads(request.content)
    assert payload["model"] == "local-model"
    assert payload["messages"][-1] == {
        "role": "user",
        "content": "Question and context",
    }
    assert payload["temperature"] == 0.25
    assert payload["max_tokens"] == 321
    assert payload["stream"] is False


@pytest.mark.asyncio
async def test_generate_adds_bearer_token_when_configured():
    captured_headers: list[httpx.Headers] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        captured_headers.append(request.headers)
        return httpx.Response(200, json=_success_response())

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        api_token="local-secret",
        retries=0,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        await service.generate(prompt="prompt", max_new_tokens=10)

    assert captured_headers[0]["Authorization"] == "Bearer local-secret"


@pytest.mark.asyncio
async def test_generate_maps_missing_model_without_exposing_provider_response():
    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="missing-model",
        retries=0,
        transport=httpx.MockTransport(
            lambda _request: httpx.Response(
                404,
                json={"error": "provider detail that must not reach the user"},
            )
        ),
    )

    with pytest.raises(LLMModelUnavailableError) as raised:
        async with service:
            await service.generate(prompt="prompt", max_new_tokens=10)

    assert "Администрирование → Модели" in str(raised.value)
    assert "provider detail" not in str(raised.value)


@pytest.mark.asyncio
async def test_stream_generate_maps_missing_model_to_configuration_error():
    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="missing-model",
        retries=0,
        transport=httpx.MockTransport(lambda _request: httpx.Response(404)),
    )

    with pytest.raises(LLMModelUnavailableError):
        async with service:
            async for _chunk in service.stream_generate(prompt="prompt", max_new_tokens=10):
                pass


@pytest.mark.asyncio
async def test_stream_generate_handles_deltas_malformed_events_and_done():
    stream_body = "\n\n".join(
        [
            'data: {"choices":[{"delta":{"content":"Hello"}}]}',
            "data:",
            "data: not-json",
            'data: {"optional":"event"}',
            'data: {"choices":[{"delta":{"content":" world"}}]}',
            "data: [DONE]",
            'data: {"choices":[{"delta":{"content":" ignored"}}]}',
        ]
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        payload = json.loads(request.content)
        assert payload["stream"] is True
        assert request.headers["Accept"] == "text/event-stream"
        return httpx.Response(
            200,
            text=stream_body,
            headers={"Content-Type": "text/event-stream"},
        )

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        retries=0,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        chunks = [
            chunk
            async for chunk in service.stream_generate(
                prompt="prompt",
                max_new_tokens=10,
            )
        ]

    assert chunks == ["Hello", " world"]


@pytest.mark.asyncio
async def test_generate_maps_429_after_retries():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(429, json={"error": "busy"})

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        retries=1,
        retry_backoff_sec=0,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        with pytest.raises(LLMRateLimitError):
            await service.generate(prompt="prompt", max_new_tokens=10)

    assert attempts == 2


@pytest.mark.asyncio
async def test_generate_maps_temporary_5xx_after_retries():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503, json={"error": "starting"})

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        retries=1,
        retry_backoff_sec=0,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        with pytest.raises(LLMTemporaryUnavailableError):
            await service.generate(prompt="prompt", max_new_tokens=10)

    assert attempts == 2


@pytest.mark.asyncio
async def test_generate_retries_timeout_then_succeeds():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise httpx.ReadTimeout("model startup timeout", request=request)
        return httpx.Response(200, json=_success_response("Recovered"))

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        retries=1,
        retry_backoff_sec=0,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        result = await service.generate(prompt="prompt", max_new_tokens=10)

    assert result == "Recovered"
    assert attempts == 2


@pytest.mark.asyncio
async def test_stream_generate_retries_temporary_status_before_emitting():
    attempts = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(503, json={"error": "loading"})
        return httpx.Response(
            200,
            text='data: {"choices":[{"delta":{"content":"Ready"}}]}\n\ndata: [DONE]\n\n',
            headers={"Content-Type": "text/event-stream"},
        )

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        retries=1,
        retry_backoff_sec=0,
        transport=httpx.MockTransport(handler),
    )

    async with service:
        chunks = [
            chunk
            async for chunk in service.stream_generate(
                prompt="prompt",
                max_new_tokens=10,
            )
        ]

    assert chunks == ["Ready"]
    assert attempts == 2


@pytest.mark.asyncio
async def test_is_configured_does_not_require_token():
    configured = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        api_token="",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    missing_url = OpenAICompatibleLLMService(
        api_base_url="",
        model="local-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    missing_model = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )

    assert configured.is_configured() is True
    assert missing_url.is_configured() is False
    assert missing_model.is_configured() is False

    await configured.aclose()
    await missing_url.aclose()
    await missing_model.aclose()


@pytest.mark.asyncio
async def test_owned_http_client_is_closed():
    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        transport=httpx.MockTransport(lambda request: httpx.Response(200)),
    )
    owned_client = service._client

    await service.aclose()

    assert owned_client.is_closed is True


@pytest.mark.asyncio
async def test_injected_http_client_is_supported_and_caller_owned():
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=_success_response("Injected"))

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        retries=0,
        client=client,
    )

    assert await service.generate(prompt="prompt", max_new_tokens=10) == "Injected"
    await service.aclose()
    assert client.is_closed is False
    await client.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "expected"),
    [
        (
            httpx.Response(200, json={"data": [{"id": "local-model"}]}),
            {"ready": True, "status": "available"},
        ),
        (
            httpx.Response(200, json={"data": [{"id": "another-model"}]}),
            {"ready": False, "status": "model_missing"},
        ),
        (httpx.Response(404), {"ready": False, "status": "unsupported"}),
    ],
)
async def test_model_readiness_available_and_unsupported(response, expected):
    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        transport=httpx.MockTransport(lambda request: response),
    )

    async with service:
        assert await service.readiness() == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("error", "status"),
    [
        (httpx.ConnectError("refused"), "unavailable"),
        (httpx.ReadTimeout("timeout"), "timeout"),
    ],
)
async def test_model_readiness_refused_and_timeout(error, status):
    def handler(request):
        error.request = request
        raise error

    service = OpenAICompatibleLLMService(
        api_base_url="http://model.test/v1",
        model="local-model",
        transport=httpx.MockTransport(handler),
    )

    async with service:
        assert await service.readiness() == {"ready": False, "status": status}
