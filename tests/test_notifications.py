from unittest.mock import AsyncMock, patch

import pytest

from app.clients.suppliers import registry
from app.clients.telegram.formatting import status_header_ru
from app.services import background
from app.services.orders import _common as common


class FakeSession:
    async def __aenter__(self):
        return None

    async def __aexit__(self, *exc):
        return False


@pytest.fixture
def sent():
    return []


@pytest.fixture
def notify(sent):
    """Прогон _notify с перехватом отправки в TG."""

    async def _notify(text, silent=False, dedupe=None, already_notified=False):
        async def send(message, silent=False):
            sent.append((message, silent))
            return True

        with (
            patch.object(common.repo, "try_mark_notified", AsyncMock(return_value=not already_notified)),
            patch.object(common.telegram, "send_message", AsyncMock(side_effect=send)),
        ):
            await common._notify(None, text, silent=silent, dedupe=dedupe)

    return _notify


async def test_notify_sends_message(notify, sent):
    await notify("привет")
    assert sent == [("привет", False)]


async def test_notify_passes_silent_flag(notify, sent):
    await notify("тихо", silent=True)
    assert sent == [("тихо", True)]


async def test_notify_dedupe_blocks_repeat(notify, sent):
    await notify("дубль", dedupe=("CODE-1", "fail"), already_notified=True)
    assert sent == []


async def test_notify_dedupe_allows_first_send(notify, sent):
    await notify("первый", dedupe=("CODE-1", "fail"))
    assert sent == [("первый", False)]


async def test_notify_survives_telegram_failure():
    # send_message ловит всё внутри и возвращает False — заказ не должен падать
    with (
        patch.object(common.repo, "try_mark_notified", AsyncMock(return_value=True)),
        patch.object(common.repo, "unmark_notified", AsyncMock()),
        patch.object(common.telegram, "send_message", AsyncMock(return_value=False)),
    ):
        await common._notify(None, "текст", dedupe=("C1", "fail"))


async def test_notify_unmarks_dedupe_when_send_fails():
    with (
        patch.object(common.repo, "try_mark_notified", AsyncMock(return_value=True)),
        patch.object(common.repo, "unmark_notified", AsyncMock()) as unmark,
        patch.object(common.telegram, "send_message", AsyncMock(return_value=False)),
    ):
        await common._notify(None, "текст", dedupe=("C1", "fail"))

    unmark.assert_awaited_once()
    assert unmark.await_args.args[1:] == ("C1", "fail")


async def test_notify_keeps_dedupe_when_send_succeeds():
    with (
        patch.object(common.repo, "try_mark_notified", AsyncMock(return_value=True)),
        patch.object(common.repo, "unmark_notified", AsyncMock()) as unmark,
        patch.object(common.telegram, "send_message", AsyncMock(return_value=True)),
    ):
        await common._notify(None, "текст", dedupe=("C1", "fail"))

    unmark.assert_not_awaited()


# ─── Фоновая проверка статуса → уведомление ──────────────────────────────────


async def _run_status_check(supplier, status, sent):
    async def send(message, silent=False):
        sent.append((message, silent))
        return True

    method = "get_task_status" if supplier == "fragment" else "get_supplier_status"
    client = getattr(registry, supplier)

    with (
        patch.object(background.settings, "STATUS_CHECK_DELAY_SECONDS", 0),
        patch.object(client, method, AsyncMock(return_value=status)),
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(background.repo, "try_mark_notified", AsyncMock(return_value=True)),
        patch.object(background.telegram, "send_message", AsyncMock(side_effect=send)),
    ):
        await background._status_check(
            "GGSEL", "CODE-1", "order-1", {"inv": 1}, "b@x.ru", "Товар", [], {"order": "order-1"}, supplier
        )


@pytest.mark.parametrize(
    "supplier,status,header,silent",
    [
        ("fragment", {"status": "success"}, "✅ ЗАКАЗ ВЫПОЛНЕН", True),
        ("fragment", {"status": "failed", "error_text": "нет средств"}, "❌ ЗАКАЗ ОТМЕНЁН", False),
        ("smm_panel", {"status": "completed"}, "✅ ЗАКАЗ ВЫПОЛНЕН", True),
        ("smm_panel", {"status": "canceled"}, "❌ ЗАКАЗ ОТМЕНЁН", False),
        ("teateagram", {"status": "completed"}, "✅ ЗАКАЗ ВЫПОЛНЕН", True),
    ],
)
async def test_status_check_notifies_on_final_status(sent, supplier, status, header, silent):
    await _run_status_check(supplier, status, sent)

    assert len(sent) == 1, "должно уйти ровно одно уведомление"
    message, is_silent = sent[0]
    assert message.startswith(header)
    assert is_silent is silent


@pytest.mark.parametrize(
    "status,header",
    [
        ("pending", "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"),
        ("processing", "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"),
        ("awaiting_balance", "💰 ОЖИДАЕТ ПОПОЛНЕНИЯ БАЛАНСА"),
    ],
)
async def test_status_check_notifies_on_intermediate_status(sent, status, header):
    # заказ ещё в работе — шлём под ключом started, финал потом дошлёт поллер
    await _run_status_check("fragment", {"status": status}, sent)
    assert len(sent) == 1
    assert sent[0][0].startswith(header)


async def test_status_check_does_not_notify_twice(sent):
    async def send(message, silent=False):
        sent.append((message, silent))
        return True

    with (
        patch.object(background.settings, "STATUS_CHECK_DELAY_SECONDS", 0),
        patch.object(registry.fragment, "get_task_status", AsyncMock(return_value={"status": "success"})),
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(background.repo, "try_mark_notified", AsyncMock(return_value=False)),  # уже слали
        patch.object(background.telegram, "send_message", AsyncMock(side_effect=send)),
    ):
        await background._status_check("GGSEL", "C1", "o-1", {}, "", "", [], {}, "fragment")

    assert sent == []


async def test_order_notified_once_across_check_and_poller(sent):
    """Разовая проверка и поллер не должны уведомить дважды по одному заказу."""
    notified: set[tuple[str, str]] = set()

    async def try_mark(session, code, kind):
        if (code, kind) in notified:
            return False
        notified.add((code, kind))
        return True

    async def send(message, silent=False):
        sent.append((message, silent))
        return True

    row = {
        "unique_code": "CODE-1",
        "supplier_order_id": "task-1",
        "supplier": "fragment",
        "supplier_status": None,
        "platform": "ggsel",
        "goods_id": "102558269",
        "inv": 1,
        "amount": 100,
        "currency": "RUB",
        "email": "b@x.ru",
        "created_at": None,
    }

    with (
        patch.object(background.settings, "STATUS_CHECK_DELAY_SECONDS", 0),
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(background.repo, "try_mark_notified", AsyncMock(side_effect=try_mark)),
        patch.object(background.telegram, "send_message", AsyncMock(side_effect=send)),
        patch.object(background.repo, "get_pending_orders", AsyncMock(return_value=[row])),
        patch.object(background.repo, "mark_order_done", AsyncMock()),
    ):
        # 1) разовая проверка застала промежуточный статус — шлёт «в процессе» под ключом started
        with patch.object(registry.fragment, "get_task_status", AsyncMock(return_value={"status": "processing"})):
            await background._status_check("GGSEL", "CODE-1", "task-1", {}, "", "Товар", [], {}, "fragment")
        assert len(sent) == 1
        assert sent[0][0].startswith("👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ")
        assert ("CODE-1", "started") in notified

        # 2) промежуточное не заняло ключ final — поллер уведомляет о финале
        with patch.object(registry.fragment, "get_task_status", AsyncMock(return_value={"status": "failed"})):
            await background._poll_orders_once()
        assert len(sent) == 2
        assert sent[1][0].startswith("❌ ЗАКАЗ ОТМЕНЁН")

        # 3) следующий цикл поллера не дублирует финал
        with patch.object(registry.fragment, "get_task_status", AsyncMock(return_value={"status": "failed"})):
            await background._poll_orders_once()
        assert len(sent) == 2


async def test_poller_closes_finished_order(sent):
    """Закрытый и уведомлённый заказ снимается с опроса, а не крутится в батче вечно."""
    row = {
        "unique_code": "CODE-DONE",
        "supplier_order_id": "ord-9",
        "supplier": "smm_panel",
        "supplier_status": '{"status": "COMPLETED"}',  # финал уже сохранён
        "platform": "ggsel",
        "goods_id": "1",
        "inv": 1,
        "amount": 100,
        "currency": "RUB",
        "email": "b@x.ru",
        "created_at": None,
    }
    mark_done = AsyncMock()
    fetch = AsyncMock(return_value={"status": "COMPLETED"})

    async def send(message, silent=False):
        sent.append((message, silent))
        return True

    with (
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "get_pending_orders", AsyncMock(return_value=[row])),
        patch.object(background.repo, "is_notified", AsyncMock(return_value=True)),  # уведомление уже ушло
        patch.object(background.repo, "mark_order_done", mark_done),
        patch.object(background.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(registry.smm_panel, "get_supplier_status", fetch),
        patch.object(background.telegram, "send_message", AsyncMock(side_effect=send)),
    ):
        await background._poll_orders_once()

    mark_done.assert_awaited_once()
    assert mark_done.await_args.args[1] == "CODE-DONE"
    assert sent == [], "повторное уведомление не нужно"
    fetch.assert_not_awaited(), "закрытый заказ не должен дёргать поставщика"


async def test_status_check_failure_does_not_crash():
    # упавший опрос статуса не должен ронять фоновую задачу
    with (
        patch.object(background.settings, "STATUS_CHECK_DELAY_SECONDS", 0),
        patch.object(registry.fragment, "get_task_status", AsyncMock(side_effect=RuntimeError("сеть"))),
    ):
        await background._status_check("GGSEL", "C1", "o-1", {}, "", "", [], {}, "fragment")


async def test_outbox_status_check_restores_purchase_context():
    row = {
        "platform": "ggsel",
        "inv": 555,
        "goods_id": "102084952",
        "amount": 100,
        "amount_usd": 1,
        "profit": 10,
        "currency": "RUB",
        "email": "buyer@example.com",
        "tg_link": "https://t.me/mychannel",
        "days": 90,
    }

    with (
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "get_by_unique_code", AsyncMock(return_value=row)),
        patch.object(background, "_status_check", AsyncMock()) as status_check,
    ):
        await background._outbox_status_check("CODE-1", "417", {"id": 417}, "smm_panel")

    status_check.assert_awaited_once()
    args = status_check.await_args.args
    assert args[0] == "GGSEL"
    assert args[1:3] == ("CODE-1", "417")
    assert args[3]["inv"] == 555
    assert args[4] == "buyer@example.com"
    assert args[6] == [
        {"name": "Ссылка", "value": "https://t.me/mychannel"},
        {"name": "Количество дней", "value": "90"},
    ]
    assert args[7] == {"id": 417}
    assert args[8] == "smm_panel"


async def test_outbox_status_check_contains_purchase_lookup_failure():
    with (
        patch.object(background, "async_session", FakeSession),
        patch.object(background.repo, "get_by_unique_code", AsyncMock(side_effect=RuntimeError("db unavailable"))),
        patch.object(background, "_status_check", AsyncMock()) as status_check,
    ):
        await background._outbox_status_check("CODE-1", "417", {"id": 417}, "smm_panel")

    status_check.assert_not_awaited()


# ─── Заголовки статусов ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "status,expected",
    [
        ({"status": "success"}, "✅ ЗАКАЗ ВЫПОЛНЕН"),
        ({"status": "failed"}, "❌ ЗАКАЗ ОТМЕНЁН"),
        ({"status": "awaiting_balance"}, "💰 ОЖИДАЕТ ПОПОЛНЕНИЯ БАЛАНСА"),
        ({"status": "pending"}, "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"),
        ({"status": "processing"}, "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"),
        ({"status": "partial"}, "⚠️ ЗАКАЗ ВЫПОЛНЕН ЧАСТИЧНО"),
        ({"status": "paused"}, "⏸️ ЗАКАЗ ПРИОСТАНОВЛЕН"),
        (None, "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"),
    ],
)
def test_status_header(status, expected):
    assert status_header_ru(status) == expected
