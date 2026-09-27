from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

import pytest

from app.services import sales_poller


class FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def sale(inv=100, minutes_ago=10):
    date = (datetime.now(UTC) - timedelta(minutes=minutes_ago)).isoformat(timespec="seconds")
    return {"invoice_id": inv, "date": date, "product": {"id": 5431904, "name": "Бусты"}}


def info(state=3, code="CODE-100"):
    return {"invoice_state": state, "name": code, "item_id": 5431904}


@pytest.fixture(autouse=True)
def _reset(monkeypatch):
    sales_poller._skipped.clear()
    monkeypatch.setattr(sales_poller.settings, "GGSEL_SALES_MIN_AGE_SECONDS", 300)
    monkeypatch.setattr(sales_poller.settings, "GGSEL_SALES_MAX_AGE_HOURS", 24)


@pytest.fixture
def poll():
    """Один цикл поллера. purchase_info — ответ или список ответов/исключений по очереди."""

    async def _poll(sales, purchase_info=None, known=(), saved=True):
        info_mock = (
            AsyncMock(side_effect=purchase_info)
            if isinstance(purchase_info, list)
            else AsyncMock(return_value=purchase_info or info())
        )
        mocks = {
            "info": info_mock,
            "process": AsyncMock(),
            "send": AsyncMock(return_value=True),
        }
        with (
            patch.object(sales_poller, "async_session", FakeSession),
            patch.object(sales_poller.ggsel, "get_token", AsyncMock(return_value="tok")),
            patch.object(sales_poller.ggsel, "get_last_sales", AsyncMock(return_value=sales)),
            patch.object(sales_poller.ggsel, "get_purchase_info", mocks["info"]),
            patch.object(sales_poller.repo, "get_known_invs", AsyncMock(return_value=set(known))),
            patch.object(sales_poller.repo, "get_by_unique_code", AsyncMock(return_value={"id": 1} if saved else None)),
            patch.object(sales_poller, "process_ggsel", mocks["process"]),
            patch.object(sales_poller.telegram, "send_message", mocks["send"]),
        ):
            await sales_poller.poll_sales_once()
        return mocks

    return _poll


async def test_unlaunched_paid_sale_is_launched_by_its_unique_code(poll):
    m = await poll([sale()])
    m["info"].assert_awaited_once_with("tok", 100)
    m["process"].assert_awaited_once()
    assert m["process"].await_args.args[1] == "CODE-100"
    assert "Автозапуск" in m["send"].await_args.args[0]


async def test_sale_already_in_db_is_not_touched(poll):
    m = await poll([sale()], known={100})
    m["info"].assert_not_awaited()
    m["process"].assert_not_awaited()


@pytest.mark.parametrize("minutes_ago", [1, 25 * 60])
async def test_too_fresh_or_too_old_sale_is_ignored(poll, minutes_ago):
    m = await poll([sale(minutes_ago=minutes_ago)])
    m["info"].assert_not_awaited()
    m["process"].assert_not_awaited()


@pytest.mark.parametrize("state", [2, 5])
async def test_canceled_or_refunded_sale_is_skipped_for_good(poll, state):
    m = await poll([sale()], purchase_info=info(state=state))
    m["process"].assert_not_awaited()
    again = await poll([sale()], purchase_info=info(state=state))
    again["info"].assert_not_awaited()


async def test_unpaid_sale_is_rechecked_next_cycle(poll):
    m = await poll([sale()], purchase_info=info(state=1))
    m["process"].assert_not_awaited()
    again = await poll([sale()], purchase_info=info(state=1))
    again["info"].assert_awaited_once()


async def test_sale_without_unique_code_is_not_launched(poll):
    m = await poll([sale()], purchase_info=info(code=""))
    m["process"].assert_not_awaited()


async def test_failed_launch_is_retried_without_notification(poll):
    m = await poll([sale()], saved=False)
    m["process"].assert_awaited_once()
    m["send"].assert_not_awaited()
    again = await poll([sale()], saved=False)
    again["process"].assert_awaited_once()


async def test_error_on_one_sale_does_not_stop_others(poll):
    m = await poll([sale(inv=100), sale(inv=200)], purchase_info=[RuntimeError("boom"), info(code="CODE-200")])
    m["process"].assert_awaited_once()
    assert m["process"].await_args.args[1] == "CODE-200"
