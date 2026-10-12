from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.clients.base import BaseApi
from app.clients.platforms.ggsel import GgselApi
from app.services import background


def http_error(status: int, headers: dict[str, str] | None = None) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://seller.ggsel.com/x")
    response = httpx.Response(status, request=request, headers=headers)
    return httpx.HTTPStatusError("err", request=request, response=response)


# ─── 429 от GGSEL ────────────────────────────────────────────────────────────


async def count_requests_on_429(api: BaseApi) -> int:
    request = httpx.Request("GET", "https://seller.ggsel.com/x")
    send = AsyncMock(return_value=httpx.Response(429, request=request))
    with (
        patch.object(api._client, "request", send),
        patch("app.clients.base.asyncio.sleep", AsyncMock()),
        pytest.raises(httpx.HTTPStatusError),
    ):
        await api.request("GET", "https://seller.ggsel.com/x")
    return send.await_count


async def test_ggsel_does_not_retry_rate_limit():
    # повтор через 0.5–2 с только добавлял запросов к исчерпанному лимиту
    assert await count_requests_on_429(GgselApi()) == 1


async def test_other_clients_still_retry_rate_limit():
    assert await count_requests_on_429(BaseApi()) == 4


@pytest.mark.parametrize(
    "error,expected",
    [
        (http_error(429, {"Retry-After": "120"}), 120),
        (http_error(429), 0),
        (http_error(429, {"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"}), 0),
        (http_error(500, {"Retry-After": "120"}), 0),
        (RuntimeError("сеть"), 0),
    ],
)
def test_retry_after(error, expected):
    assert background.retry_after(error) == expected


# ─── пауза между циклами ─────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "interval,fails,expected",
    [(15, 0, 15), (15, 1, 30), (15, 3, 120), (15, 5, 300), (15, 50, 300), (60, 1, 120), (600, 3, 600)],
)
def test_poll_delay_backs_off_on_errors(interval, fails, expected):
    assert background.poll_delay(interval, fails) == expected


# ─── поллер чатов ────────────────────────────────────────────────────────────


class FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def chat(id_i, last_message=None):
    return {"id_i": id_i, "last_message": last_message or datetime.now(UTC).isoformat()}


@pytest.fixture(autouse=True)
def clean_state():
    background._chat_seen.clear()
    background._full_scan_at = None
    yield
    background._chat_seen.clear()
    background._full_scan_at = None


async def poll_chats(items, process, pages=None):
    pages = pages or AsyncMock(side_effect=lambda token, filter_new, page: {"items": items if page == 1 else []})
    with (
        patch.object(background.ggsel, "get_token", AsyncMock(return_value="tok")),
        patch.object(background.ggsel, "get_chats", pages),
        patch.object(background, "async_session", FakeSession),
        patch.object(background, "_process_chat", process),
    ):
        await background._poll_once()
    return pages


def filters(pages):
    """filter_new первой страницы каждого цикла."""
    return [c.kwargs["filter_new"] for c in pages.await_args_list if c.kwargs["page"] == 1]


async def test_full_scan_then_only_unread_chats(monkeypatch):
    monkeypatch.setattr(background.settings, "GGSEL_CHAT_FULL_SCAN_INTERVAL", 600)
    pages = AsyncMock(return_value={"items": []})
    now = [0.0]
    # подменяем только часы модуля: time.monotonic нужен ещё и event loop
    monkeypatch.setattr(background, "time", SimpleNamespace(monotonic=lambda: now[0]))

    for now[0] in (1000.0, 1010.0, 1300.0, 1700.0):
        await poll_chats([], AsyncMock(), pages)

    # старт → полный; через 10 с и 300 с — только непрочитанные; через 700 с — снова полный
    assert filters(pages) == [0, 1, 1, 0]


async def test_failed_full_scan_is_repeated(monkeypatch):
    monkeypatch.setattr(background.settings, "GGSEL_CHAT_FULL_SCAN_INTERVAL", 600)
    failing = AsyncMock(side_effect=http_error(429))
    with pytest.raises(httpx.HTTPStatusError):
        await poll_chats([], AsyncMock(), failing)

    pages = await poll_chats([], AsyncMock())
    assert filters(pages) == [0]  # прерванный полный проход не засчитан


async def test_zero_interval_keeps_full_scan_every_cycle(monkeypatch):
    monkeypatch.setattr(background.settings, "GGSEL_CHAT_FULL_SCAN_INTERVAL", 0)
    pages = AsyncMock(return_value={"items": []})

    for _ in range(3):
        await poll_chats([], AsyncMock(), pages)

    assert filters(pages) == [0, 0, 0]


async def test_unchanged_chat_is_not_refetched():
    items = [chat(1), chat(2)]
    process = AsyncMock(return_value=True)

    await poll_chats(items, process)
    await poll_chats(items, process)
    assert process.await_count == 2  # только в первом цикле

    items[1]["last_message"] = "2099-01-01T00:00:00+03:00"  # новое сообщение во втором чате
    await poll_chats(items, process)
    assert process.await_count == 3
    assert process.await_args.args[2]["id_i"] == 2


async def test_chat_with_unsent_messages_is_retried():
    items = [chat(1)]
    process = AsyncMock(return_value=False)  # TG не принял сообщение

    await poll_chats(items, process)
    await poll_chats(items, process)

    assert process.await_count == 2


async def test_rate_limit_aborts_chat_cycle():
    process = AsyncMock(side_effect=http_error(429))

    with pytest.raises(httpx.HTTPStatusError):
        await poll_chats([chat(1), chat(2), chat(3)], process)

    process.assert_awaited_once()  # остальные чаты не трогаем


async def test_other_chat_errors_do_not_stop_the_cycle():
    process = AsyncMock(side_effect=[http_error(500), True])

    await poll_chats([chat(1), chat(2)], process)

    assert process.await_count == 2
