import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.orders import _common as common, digiseller as digi_orders
from app.services.orders.digiseller import process_digiseller

LINK_OPT = {"name": "Ссылка", "value": "https://t.me/mychannel"}
DAYS_OPT = {"name": "Количество дней", "value": "90"}


def purchase(goods_id="5558693", options=None, **over):
    row = {
        "retval": 0,
        "inv": 777,
        "id_goods": goods_id,
        "email": "buyer@example.com",
        "cnt_goods": "1",
        "amount": 200,
        "type_curr": "RUB",
        "options": options if options is not None else [LINK_OPT, DAYS_OPT],
    }
    row.update(over)
    return row


class Run:
    def __init__(self):
        self.rows = []
        self.scheduled = []
        self.smm = None
        self.processed = []
        self.released = []
        self.saved_orders = []

    @property
    def row(self):
        assert self.rows, "в purchases ничего не записано"
        return self.rows[0]

    @property
    def error_message(self):
        return json.loads(self.row["supplier_status"])["message"]


@pytest.fixture
def run():
    async def _run(data, *, is_processed=False, lock_free=True, supplier_resp=None, supplier_error=None):
        r = Run()

        async def smm(service, link, qty):
            r.smm = (service, link, qty)
            if supplier_error:
                raise supplier_error
            return supplier_resp if supplier_resp is not None else {"order": "smm-1"}

        with (
            patch.object(digi_orders.repo, "get_by_unique_code", AsyncMock(return_value=None)),
            patch.object(digi_orders.repo, "is_processed", AsyncMock(return_value=is_processed)),
            patch.object(digi_orders.repo, "try_acquire_inflight_inv", AsyncMock(return_value=lock_free)),
            patch.object(digi_orders.repo, "mark_processed", AsyncMock(side_effect=lambda s, inv: r.processed.append(inv))),
            patch.object(
                digi_orders.repo, "release_inflight_inv", AsyncMock(side_effect=lambda s, inv: r.released.append(inv))
            ),
            patch.object(
                digi_orders.repo,
                "save_supplier_order",
                AsyncMock(side_effect=lambda s, inv, oid: r.saved_orders.append((inv, oid))),
            ),
            patch.object(digi_orders.repo, "insert_purchase", AsyncMock(side_effect=lambda s, row: r.rows.append(row))),
            patch.object(common.repo, "insert_purchase", AsyncMock(side_effect=lambda s, row: r.rows.append(row))),
            patch.object(common.repo, "try_mark_notified", AsyncMock(return_value=True)),
            patch.object(common.telegram, "send_message", AsyncMock(return_value=True)),
            patch.object(digi_orders.digiseller, "get_purchase", AsyncMock(return_value=data)),
            patch.object(digi_orders.smm_panel, "create_supplier_order", AsyncMock(side_effect=smm)),
            patch.object(
                digi_orders.background,
                "schedule_status_check",
                lambda *a, **k: r.scheduled.append(a[2]),
            ),
        ):
            await process_digiseller(None, "CODE-D")
        return r

    return _run


async def test_order_accepted(run):
    r = await run(purchase())

    assert r.smm == ("G_BOOST_90", "https://t.me/mychannel", 1)
    assert r.row["status"] == "SUPPLIER_ACCEPTED"
    assert r.row["platform"] == "digiseller"
    assert r.row["supplier"] == "smm_panel"
    assert r.row["supplier_order_id"] == "smm-1"
    assert r.scheduled == ["smm-1"]
    assert r.saved_orders == [(777, "smm-1")]
    assert r.processed == [777]  # PLATI не должна ретраить после add


async def test_content_shape_is_normalized(run):
    # Digiseller отдаёт заказ либо плоско, либо во вложенном content
    data = {
        "retval": 0,
        "content": {
            "external_order_id": 999,
            "id_goods": "5558693",
            "buyer_info": {"email": "nested@example.com"},
            "cnt_goods": "1",
            "amount": 300,
            "options": [LINK_OPT, DAYS_OPT],
        },
    }
    r = await run(data)

    assert r.row["status"] == "SUPPLIER_ACCEPTED"
    assert r.row["email"] == "nested@example.com"
    assert r.row["inv"] == 999


async def test_retval_error_saves_and_notifies(run):
    r = await run(purchase(retval=1, retdesc="код не найден"))

    assert r.smm is None
    assert r.row["status"] == "ERROR"
    assert "retval=1" in r.error_message


async def test_unknown_goods_without_days_rejected(run):
    r = await run(purchase(goods_id="000", options=[LINK_OPT]))

    assert r.smm is None
    assert r.row["status"] == "ERROR"
    assert "service_id" in r.error_message
    assert r.processed == [777]  # инвойс закрыт, ретраить нечего


async def test_invalid_link_rejected(run):
    r = await run(purchase(options=[{"name": "Ссылка", "value": "мусор"}, DAYS_OPT]))

    assert r.smm is None
    assert r.row["status"] == "ERROR_INVALID_LINK"
    assert r.processed == [777]


async def test_supplier_without_order_id_releases_lock_for_retry(run):
    r = await run(purchase(), supplier_resp={"detail": "нет id"})

    assert r.row["status"] == "ERROR"
    assert "order_id" in r.error_message
    assert r.released == [777]
    assert r.processed == []  # НЕ помечаем processed — заказ можно повторить


async def test_order_id_taken_from_alternative_keys(run):
    r = await run(purchase(), supplier_resp={"id": "alt-1"})

    assert r.row["supplier_order_id"] == "alt-1"
    assert r.scheduled == ["alt-1"]


async def test_already_processed_invoice_skipped(run):
    r = await run(purchase(), is_processed=True)

    assert r.smm is None
    assert r.rows == []


async def test_locked_invoice_skipped(run):
    r = await run(purchase(), lock_free=False)

    assert r.smm is None
    assert r.rows == []


async def test_supplier_exception_notifies_without_row(run):
    r = await run(purchase(), supplier_error=RuntimeError("поставщик лёг"))

    # покупка не записана — клиент увидит 404 на /status
    assert r.rows == []
    assert r.scheduled == []


async def test_existing_order_not_processed_twice():
    with (
        patch.object(digi_orders.repo, "get_by_unique_code", AsyncMock(return_value={"unique_code": "CODE-D"})),
        patch.object(digi_orders.digiseller, "get_purchase", AsyncMock()) as get_purchase,
    ):
        assert await process_digiseller(None, "CODE-D") == "CODE-D"
    get_purchase.assert_not_awaited()
