import httpx
import pytest

from app.retrieval.reranker import RerankerClient


@pytest.mark.asyncio
async def test_reranker_reorders_and_falls_back_safely():
    def handler(request):
        return httpx.Response(
            200,
            request=request,
            json={"results": [{"index": 1, "relevance_score": 0.9}]},
        )

    client = RerankerClient("http://reranker.test", None, 1, transport=httpx.MockTransport(handler))
    docs = [{"text": "a", "doc_id": "a"}, {"text": "b", "doc_id": "b"}]
    assert (await client.rerank("q", docs, 1))[0]["doc_id"] == "b"
    await client.close()

    def fail(request):
        return httpx.Response(503, request=request)

    fallback = RerankerClient("http://reranker.test", None, 1, transport=httpx.MockTransport(fail))
    assert await fallback.rerank("q", docs, 1) == docs[:1]
    await fallback.close()
