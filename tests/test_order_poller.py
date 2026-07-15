from datetime import UTC, datetime, timedelta
import json
from unittest.mock import AsyncMock, patch

import pytest

from app.clients.suppliers import registry
from app.services import background


class FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


def order(**over):
    row = {
        "unique_code": "CODE-1",
        "supplier_order_id": "task-1",
        "supplier": "fragment",
        "supplier_status": None,
        "platform": "ggsel",
        "goods_id": "102558269",
        "inv": 555,
        "amount": 100,
        "currency": "RUB",
        "email": "b@x.ru",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds"),
    }
    row.update(over)
    return row


@pytest.fixture
def poll():
    """Один цикл поллера. Возвращает (отправленные сообщения, сохранённые статусы)."""

    async def _poll(rows, status, notified_ok=True):
        sent, saved = [], []

        async def send(message, silent=False):
            sent.append((message, silent))
            return True

        async def update(session, code, payload):
            saved.append((code, payload))

        with (
            patch.object(background, "async_session", FakeSession),
            patch.object(background.repo, "get_pending_orders", AsyncMock(return_value=rows)),
            patch.object(background.repo, "update_supplier_by_ucode", AsyncMock(side_effect=update)),
            patch.object(background.repo, "try_mark_notified", AsyncMock(return_value=notified_ok)),
            patch.object(background.telegram, "send_message", AsyncMock(side_effect=send)),
            patch.object(registry.fragment, "get_task_status", AsyncMock(return_value=status)),
            patch.object(registry.smm_panel, "get_supplier_status", AsyncMock(return_value=status)),
        ):
            await background._poll_orders_once()
        return sent, saved

    return _poll


async def test_final_status_notifies_and_saves(poll):
    sent, saved = await poll([order()], {"status": "success"})

    assert len(sent) == 1
    assert sent[0][0].startswith("✅ ЗАКАЗ ВЫПОЛНЕН")
    assert sent[0][1] is True  # успех — тихо
    assert saved and json.loads(saved[0][1]) == {"status": "success"}


async def test_failed_status_notifies_loudly(poll):
    sent, _ = await poll([order()], {"status": "failed", "error_text": "нет средств"})

    assert len(sent) == 1
    assert sent[0][0].startswith("❌ ЗАКАЗ ОТМЕНЁН")
    assert sent[0][1] is False


@pytest.mark.parametrize("status", ["pending", "processing", "awaiting_balance"])
async def test_intermediate_status_saved_but_no_spam(poll, status):
    # поллер крутится каждую минуту — уведомлять на промежуточных статусах нельзя
    sent, saved = await poll([order()], {"status": status})

    assert sent == []
    assert saved and json.loads(saved[0][1]) == {"status": status}


async def test_already_final_order_is_not_polled(poll):
    row = order(supplier_status=json.dumps({"status": "success"}))
    sent, saved = await poll([row], {"status": "success"})

    assert sent == []
    assert saved == []  # к поставщику не ходили


async def test_stale_order_is_dropped(poll):
    old = (datetime.now(UTC) - timedelta(hours=100)).isoformat(timespec="seconds")
    sent, saved = await poll([order(created_at=old)], {"status": "processing"})

    assert sent == []
    assert saved == []


async def test_already_notified_order_does_not_resend(poll):
    sent, saved = await poll([order()], {"status": "success"}, notified_ok=False)

    assert sent == []
    assert saved  # статус всё равно сохраняем


async def test_smm_panel_order_polled_too(poll):
    row = order(supplier="smm_panel", supplier_order_id="smm-1", goods_id="102084952", platform="ggsel")
    sent, _ = await poll([row], {"status": "completed"})

    assert len(sent) == 1
    assert sent[0][0].startswith("✅ ЗАКАЗ ВЫПОЛНЕН")


async def test_broken_row_does_not_stop_the_batch(poll):
    rows = [order(unique_code=""), order(unique_code="CODE-2")]
    sent, _ = await poll(rows, {"status": "success"})

    # первая строка пропущена, вторая обработана
    assert len(sent) == 1


async def test_supplier_error_does_not_stop_the_batch():
    sent = []

    async def send(message, silent=False):
        sent.append(message)
        return True

    rows = [order(unique_code="BAD"), order(unique_code="GOOD", supplier="smm_panel", supplier_order_id="smm-1")]

    with (
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "get_pending_orders", AsyncMock(return_value=rows)),
        patch.object(background.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(background.repo, "try_mark_notified", AsyncMock(return_value=True)),
        patch.object(background.telegram, "send_message", AsyncMock(side_effect=send)),
        patch.object(registry.fragment, "get_task_status", AsyncMock(side_effect=RuntimeError("сеть"))),
        patch.object(registry.smm_panel, "get_supplier_status", AsyncMock(return_value={"status": "completed"})),
    ):
        await background._poll_orders_once()

    assert len(sent) == 1  # упавший заказ не сорвал цикл


def test_final_statuses_cover_both_suppliers():
    for st in ("success", "failed"):  # Fragment
        assert st in background.FINAL_STATUSES
    for st in ("completed", "canceled", "refunded"):  # SMM Panel
        assert st in background.FINAL_STATUSES
