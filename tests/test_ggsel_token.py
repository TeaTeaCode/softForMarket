import base64
import json
import time
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.clients.base import BaseApi
from app.clients.platforms.ggsel import GgselApi, _token_expiry


def jwt(exp: float) -> str:
    payload = base64.urlsafe_b64encode(json.dumps({"exp": int(exp)}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{payload}.c2lnbmF0dXJl"


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://seller.ggsel.com/x")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(status, request=request))


# ─── токен GGSEL ─────────────────────────────────────────────────────────────


async def test_token_is_reused_until_expiry():
    api = GgselApi()
    login = AsyncMock(return_value={"retval": 0, "token": jwt(time.time() + 31 * 86400)})
    with patch.object(BaseApi, "request", login):
        first = await api.get_token()
        second = await api.get_token()

    assert first == second
    login.assert_awaited_once()  # раньше логинились на каждом цикле поллера


@pytest.mark.parametrize("status", [401, 403])
async def test_rejected_token_triggers_relogin(status):
    api = GgselApi()
    login = AsyncMock(return_value={"retval": 0, "token": jwt(time.time() + 86400)})
    with patch.object(BaseApi, "request", login):
        await api.get_token()
    with patch.object(BaseApi, "request", AsyncMock(side_effect=http_error(status))), pytest.raises(httpx.HTTPStatusError):
        await api.get_chats("tok")
    with patch.object(BaseApi, "request", login):
        await api.get_token()

    assert login.await_count == 2


async def test_rate_limit_keeps_token():
    api = GgselApi()
    login = AsyncMock(return_value={"retval": 0, "token": jwt(time.time() + 86400)})
    with patch.object(BaseApi, "request", login):
        await api.get_token()
    with patch.object(BaseApi, "request", AsyncMock(side_effect=http_error(429))), pytest.raises(httpx.HTTPStatusError):
        await api.get_chats("tok")
    with patch.object(BaseApi, "request", login):
        await api.get_token()

    login.assert_awaited_once()


def test_token_expiry_from_jwt():
    now = 1_000_000.0
    assert _token_expiry(jwt(now + 31 * 86400), now) == now + 31 * 86400 - 3600
    assert _token_expiry("не-jwt", now) == now + 3600
