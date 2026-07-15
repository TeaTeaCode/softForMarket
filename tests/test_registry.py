from unittest.mock import AsyncMock, patch

import pytest

from app.clients.suppliers import registry


@pytest.mark.parametrize(
    "supplier,client_attr,method",
    [
        ("fragment", "fragment", "get_task_status"),
        ("smm_panel", "smm_panel", "get_supplier_status"),
        ("teateagram", "teateagram", "get_supplier_status"),
    ],
)
async def test_fetch_status_dispatches_to_its_supplier(supplier, client_attr, method):
    client = getattr(registry, client_attr)
    with patch.object(client, method, AsyncMock(return_value={"status": "ok"})) as m:
        assert await registry.fetch_status(supplier, "order-1") == {"status": "ok"}
    m.assert_awaited_once_with("order-1")


async def test_fetch_status_does_not_call_other_suppliers():
    # legacy-заказ не должен уходить в smm_panel (был такой баг в background.py)
    with (
        patch.object(registry.teateagram, "get_supplier_status", AsyncMock(return_value={"status": "ok"})) as tea,
        patch.object(registry.smm_panel, "get_supplier_status", AsyncMock()) as smm,
        patch.object(registry.fragment, "get_task_status", AsyncMock()) as frg,
    ):
        await registry.fetch_status("teateagram", "old-order")

    assert tea.await_count == 1
    assert smm.await_count == 0
    assert frg.await_count == 0


@pytest.mark.parametrize("supplier", ["", "опечатка", "SMM_PANEL", "Fragment"])
async def test_fetch_status_unknown_supplier_raises(supplier):
    with pytest.raises(RuntimeError, match="Неизвестный поставщик"):
        await registry.fetch_status(supplier, "order-1")


async def test_fetch_status_error_mentions_supplier_and_order():
    with pytest.raises(RuntimeError) as exc:
        await registry.fetch_status("нет-такого", "order-99")
    assert "нет-такого" in str(exc.value)
    assert "order-99" in str(exc.value)


def test_registry_covers_every_supplier_written_to_db():
    # значения, которые пишутся в purchases.supplier
    assert set(registry.STATUS_FETCHERS) == {"fragment", "smm_panel", "teateagram"}
