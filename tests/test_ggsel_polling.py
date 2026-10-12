from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from app.services import background


def http_error(status: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://seller.ggsel.com/x")
    return httpx.HTTPStatusError("err", request=request, response=httpx.Response(status, request=request))


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
def clean_seen():
    background._chat_seen.clear()
    yield
    background._chat_seen.clear()


async def poll_chats(items, process):
    pages = AsyncMock(side_effect=lambda token, filter_new, page: {"items": items if page == 1 else []})
    with (
        patch.object(background.ggsel, "get_token", AsyncMock(return_value="tok")),
        patch.object(background.ggsel, "get_chats", pages),
        patch.object(background, "async_session", FakeSession),
        patch.object(background, "_process_chat", process),
    ):
        await background._poll_once()


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
