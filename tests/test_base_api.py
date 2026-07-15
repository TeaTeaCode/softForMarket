from unittest.mock import patch

import httpx
import pytest

from app.clients.base import RETRY_STATUSES, RETRY_TOTAL, BaseApi


def api(handler):
    client = BaseApi()
    client._client = httpx.AsyncClient(base_url="https://api.test", transport=httpx.MockTransport(handler))
    return client


@pytest.fixture(autouse=True)
def no_sleep():
    # ретраи спят экспоненциально — в тестах это лишнее
    async def instant(_):
        return None

    with patch("app.clients.base.asyncio.sleep", instant):
        yield


async def test_returns_json_by_default():
    client = api(lambda r: httpx.Response(200, json={"ok": True}))
    assert await client.request("GET", "/x") == {"ok": True}


async def test_returns_text():
    client = api(lambda r: httpx.Response(200, text="привет"))
    assert await client.request("GET", "/x", response_type="text") == "привет"


async def test_returns_raw_response():
    client = api(lambda r: httpx.Response(201, json={}))
    resp = await client.request("GET", "/x", response_type="response")
    assert resp.status_code == 201


@pytest.mark.parametrize("status", sorted(RETRY_STATUSES))
async def test_retries_on_retryable_status_then_succeeds(status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] == 1:
            return httpx.Response(status, text="временно недоступен")
        return httpx.Response(200, json={"ok": True})

    client = api(handler)
    assert await client.request("GET", "/x") == {"ok": True}
    assert calls["n"] == 2


async def test_gives_up_after_retry_budget():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(503, text="лежит")

    client = api(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.request("GET", "/x")
    assert calls["n"] == RETRY_TOTAL + 1


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
async def test_client_errors_are_not_retried(status):
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        return httpx.Response(status, json={"detail": "нет"})

    client = api(handler)
    with pytest.raises(httpx.HTTPStatusError):
        await client.request("GET", "/x")
    assert calls["n"] == 1, "ошибки клиента ретраить бессмысленно"


async def test_retries_on_transport_error():
    calls = {"n": 0}

    def handler(request):
        calls["n"] += 1
        if calls["n"] < 3:
            raise httpx.ConnectError("сеть недоступна")
        return httpx.Response(200, json={"ok": True})

    client = api(handler)
    assert await client.request("GET", "/x") == {"ok": True}
    assert calls["n"] == 3


async def test_transport_error_propagates_after_budget():
    def handler(request):
        raise httpx.ConnectTimeout("таймаут")

    client = api(handler)
    with pytest.raises(httpx.TransportError):
        await client.request("GET", "/x")


async def test_params_and_headers_are_sent():
    seen = {}

    def handler(request):
        seen["params"] = dict(request.url.params)
        seen["auth"] = request.headers.get("authorization")
        return httpx.Response(200, json={})

    client = api(handler)
    await client.request("GET", "/x", params={"a": "1"}, headers={"Authorization": "Bearer t"})

    assert seen["params"] == {"a": "1"}
    assert seen["auth"] == "Bearer t"


async def test_form_data_is_sent():
    seen = {}

    def handler(request):
        seen["body"] = request.content
        return httpx.Response(200, json={})

    client = api(handler)
    await client.request("POST", "/x", data={"action": "status"})

    assert b"action=status" in seen["body"]
