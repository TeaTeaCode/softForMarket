import json
from unittest.mock import AsyncMock, patch

import pytest

from app.services.orders import _common as common, fragment as fragment_orders, ggsel as ggsel_orders
from app.services.orders.ggsel import process_ggsel

LINK_OPT = {"name": "Ссылка", "value": "https://t.me/durov"}


def months_opt(value):
    return {"name": "Количество месяцев подписки", "value": value}


def purchase(goods_id, options, cnt="1"):
    return {
        "retval": 0,
        "inv": 555,
        "id_goods": goods_id,
        "email": "buyer@example.com",
        "cnt_goods": cnt,
        "amount": 100,
        "type_curr": "RUB",
        "options": options,
    }


class Run:
    """Результат прогона process_ggsel: что записано в БД и что ушло поставщику."""

    def __init__(self):
        self.rows = []
        self.scheduled = []
        self.stars = None
        self.premium = None
        self.smm = None

    @property
    def row(self):
        assert self.rows, "в purchases ничего не записано"
        return self.rows[0]

    @property
    def error_message(self):
        return json.loads(self.row["supplier_status"])["message"]


@pytest.fixture
def run(monkeypatch):
    async def _run(data, username_ok=True):
        r = Run()

        async def stars(username, quantity):
            r.stars = (username, quantity)
            return "task-STARS"

        async def premium(username, months):
            r.premium = (username, months)
            return "task-PREM"

        async def smm(service, link, qty):
            r.smm = (service, link, qty)
            return {"order": "smm-1"}

        with (
            patch.object(ggsel_orders.repo, "get_by_unique_code", AsyncMock(return_value=None)),
            patch.object(ggsel_orders.repo, "mark_inflight", AsyncMock(return_value=True)),
            patch.object(ggsel_orders.repo, "clear_inflight", AsyncMock()),
            patch.object(common.repo, "try_mark_notified", AsyncMock(return_value=True)),
            patch.object(common.telegram, "send_message", AsyncMock(return_value=True)),
            patch.object(ggsel_orders.repo, "insert_purchase", AsyncMock(side_effect=lambda s, row: r.rows.append(row))),
            patch.object(fragment_orders.repo, "insert_purchase", AsyncMock(side_effect=lambda s, row: r.rows.append(row))),
            patch.object(common.repo, "insert_purchase", AsyncMock(side_effect=lambda s, row: r.rows.append(row))),
            patch.object(ggsel_orders.ggsel, "get_purchase", AsyncMock(return_value=data)),
            patch.object(fragment_orders.fragment, "check_username", AsyncMock(return_value=username_ok)),
            patch.object(fragment_orders.fragment, "create_stars_order", AsyncMock(side_effect=stars)),
            patch.object(fragment_orders.fragment, "create_premium_order", AsyncMock(side_effect=premium)),
            patch.object(ggsel_orders.smm_panel, "create_supplier_order", AsyncMock(side_effect=smm)),
            patch.object(
                fragment_orders.background,
                "schedule_status_check",
                lambda *a, **k: r.scheduled.append((a[2], k.get("supplier"))),
            ),
            patch.object(
                ggsel_orders.background,
                "schedule_status_check",
                lambda *a, **k: r.scheduled.append((a[2], k.get("supplier", "smm_panel"))),
            ),
        ):
            await process_ggsel(None, "CODE-1")
        return r

    return _run


# ─── Stars ───────────────────────────────────────────────────────────────────


async def test_stars_order_accepted(run):
    r = await run(purchase("102558269", [LINK_OPT], cnt="50"))

    assert r.stars == ("durov", 50)  # количество звёзд = cnt_goods
    assert r.row["status"] == "SUPPLIER_ACCEPTED"
    assert r.row["supplier"] == "fragment"
    assert r.row["supplier_order_id"] == "task-STARS"
    assert r.row["tg_link"] == "https://t.me/durov"
    assert r.scheduled == [("task-STARS", "fragment")]


async def test_stars_rejected_when_username_not_found(run):
    r = await run(purchase("102558269", [LINK_OPT]), username_ok=False)

    assert r.stars is None  # заказ поставщику не ушёл
    assert r.row["status"] == "ERROR"
    assert r.row["supplier"] is None
    assert "@durov" in r.error_message
    assert r.scheduled == []


async def test_stars_rejected_on_invite_link(run):
    r = await run(purchase("102558269", [{"name": "Ссылка", "value": "https://t.me/+invite123"}]))

    assert r.stars is None
    assert r.row["status"] == "ERROR_INVALID_LINK"


# ─── Premium ─────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("value,expected", [("3 месяца", 3), ("6 месяцев", 6), ("12 месяцев", 12)])
async def test_premium_order_accepted(run, value, expected):
    r = await run(purchase("102558303", [LINK_OPT, months_opt(value)]))

    assert r.premium == ("durov", expected)
    assert r.row["status"] == "SUPPLIER_ACCEPTED"
    assert r.row["supplier"] == "fragment"
    assert r.row["days"] == expected  # месяцы кладём в days
    assert r.scheduled == [("task-PREM", "fragment")]


async def test_premium_rejected_without_months(run):
    r = await run(purchase("102558303", [LINK_OPT]))

    assert r.premium is None
    assert r.row["status"] == "ERROR"
    assert "срок подписки" in r.error_message


async def test_premium_rejected_on_unsupported_months(run):
    # Fragment принимает только 3/6/12 — отбиваем до вызова API
    r = await run(purchase("102558303", [LINK_OPT, months_opt("5 месяцев")]))

    assert r.premium is None
    assert r.row["status"] == "ERROR"
    assert "[3, 6, 12]" in r.error_message


# ─── Бусты (регрессия: Fragment не должен их перехватывать) ──────────────────


async def test_boost_still_goes_to_smm_panel(run):
    data = purchase(
        "102084952",
        [{"name": "Ссылка", "value": "https://t.me/mychannel"}, {"name": "Количество дней", "value": "90"}],
    )
    r = await run(data)

    assert r.smm == ("G_BOOST_90", "https://t.me/mychannel", 1)
    assert r.stars is None and r.premium is None
    assert r.row["status"] == "SUPPLIER_ACCEPTED"
    assert r.row["supplier"] == "smm_panel"
    assert r.row["days"] == 90
    assert r.scheduled == [("smm-1", "smm_panel")]


async def test_boost_invalid_link_rejected(run):
    data = purchase("102084952", [{"name": "Ссылка", "value": "не ссылка"}, {"name": "Количество дней", "value": "90"}])
    r = await run(data)

    assert r.smm is None
    assert r.row["status"] == "ERROR_INVALID_LINK"


# ─── Идемпотентность ─────────────────────────────────────────────────────────


async def test_existing_order_is_not_processed_twice():
    with (
        patch.object(ggsel_orders.repo, "get_by_unique_code", AsyncMock(return_value={"unique_code": "CODE-1"})),
        patch.object(ggsel_orders.ggsel, "get_purchase", AsyncMock()) as get_purchase,
        patch.object(ggsel_orders.repo, "clear_inflight", AsyncMock()),
    ):
        assert await process_ggsel(None, "CODE-1") == "CODE-1"
    get_purchase.assert_not_awaited()


async def test_inflight_lock_blocks_parallel_processing():
    with (
        patch.object(ggsel_orders.repo, "get_by_unique_code", AsyncMock(return_value=None)),
        patch.object(ggsel_orders.repo, "mark_inflight", AsyncMock(return_value=False)),
        patch.object(ggsel_orders.ggsel, "get_purchase", AsyncMock()) as get_purchase,
        patch.object(ggsel_orders.repo, "clear_inflight", AsyncMock()),
    ):
        await process_ggsel(None, "CODE-1")
    get_purchase.assert_not_awaited()
