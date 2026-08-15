import json
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from app.api.v1.routes import status as status_route
from app.clients.suppliers import registry
from app.db.session import get_session
from app.main import app


def row(**over):
    base = {
        "unique_code": "CODE-1",
        "goods_id": "102084952",
        "platform": "ggsel",
        "days": 90,
        "quantity": 1,
        "tg_link": "https://t.me/mychannel",
        "supplier": "smm_panel",
        "supplier_order_id": "smm-1",
        "supplier_status": None,
        "status": "SUPPLIER_ACCEPTED",
    }
    base.update(over)
    return base


@pytest.fixture
def client():
    async def fake_session():
        yield None

    app.dependency_overrides[get_session] = fake_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def get_status(client, db_row, fresh=None, fetch_error=None):
    fetch = AsyncMock(side_effect=fetch_error) if fetch_error else AsyncMock(return_value=fresh)
    with (
        patch.object(status_route.repo, "get_by_unique_code", AsyncMock(return_value=db_row)),
        patch.object(status_route.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(status_route, "fetch_status", fetch),
    ):
        return client.get("/status?code=CODE-1")


def test_missing_code_returns_400(client):
    assert client.get("/status").status_code == 400


def test_unknown_order_returns_404(client):
    r = get_status(client, None)
    assert r.status_code == 404
    assert "не найден" in r.text


def test_invalid_link_order_shows_support_message(client):
    r = get_status(client, row(status="ERROR_INVALID_LINK"))

    assert r.status_code == 200
    assert "неверная ссылка" in r.text.lower()
    # финальное состояние — страница не должна сама перезагружаться
    assert 'http-equiv="refresh"' not in r.text


def test_terminal_supplier_delivery_problem_shows_support_without_refresh(client):
    r = get_status(client, row(status="OUTBOX_DELIVERY_ERROR", supplier_order_id=None))

    assert r.status_code == 200
    assert "Заказ был отменён системой! Пожалуйста, свяжитесь с поддержкой." in r.text
    assert "поставщик" not in r.text.lower()
    assert 'http-equiv="refresh"' not in r.text


def stars_row(**over):
    base = row(
        goods_id="102558269",
        days=0,
        quantity=50,
        tg_link="https://t.me/cxpykat",
        supplier="fragment",
        supplier_order_id="task-1",
    )
    base.update(over)
    return base


def test_stars_page_has_no_boost_wording(client):
    r = get_status(client, stars_row(), fresh={"status": "processing"})

    assert r.status_code == 200
    assert "количество дней" not in r.text.lower()
    assert "осталось накрутить" not in r.text.lower()
    assert "ссылка тг" not in r.text.lower()


def test_stars_page_shows_star_count_and_recipient(client):
    r = get_status(client, stars_row(), fresh={"status": "processing"})

    assert "количество звёзд" in r.text.lower()
    assert ">50<" in r.text
    assert "получатель" in r.text.lower()
    assert "https://t.me/cxpykat" in r.text
    assert "Telegram Stars" in r.text


def test_premium_page_shows_months_not_star_count(client):
    r = get_status(client, stars_row(goods_id="102558303", days=6, quantity=1), fresh={"status": "processing"})

    assert "срок подписки" in r.text.lower()
    assert "6 мес." in r.text
    assert "количество звёзд" not in r.text.lower()
    assert "Telegram Premium" in r.text


def test_boost_page_keeps_boost_wording(client):
    r = get_status(client, row(), fresh={"status": "processing", "remains": 3})

    assert "количество дней" in r.text.lower()
    assert "осталось накрутить" in r.text.lower()
    assert "количество звёзд" not in r.text.lower()


def test_fragment_invalid_username_uses_fragment_template(client):
    r = get_status(client, stars_row(status="ERROR_INVALID_USERNAME"))

    assert "@username" in r.text
    assert "осталось накрутить" not in r.text.lower()


def test_invalid_username_order_shows_username_message(client):
    r = get_status(client, row(status="ERROR_INVALID_USERNAME"))

    assert r.status_code == 200
    # Stars/Premium покупают на аккаунт — про ссылку на канал писать нельзя
    assert "@username" in r.text
    assert "ссылка на канал" not in r.text.lower()
    assert 'http-equiv="refresh"' not in r.text


def test_uniquecode_alias_works(client):
    with (
        patch.object(status_route.repo, "get_by_unique_code", AsyncMock(return_value=row(status="ERROR_INVALID_LINK"))),
    ):
        assert client.get("/status?uniquecode=CODE-1").status_code == 200


def test_completed_order_shows_done_without_refresh(client):
    db_row = row(supplier_status=json.dumps({"status": "completed"}))
    r = get_status(client, db_row)

    assert "Завершён" in r.text
    assert 'http-equiv="refresh"' not in r.text


def test_in_progress_order_refreshes(client):
    db_row = row(supplier_status=json.dumps({"status": "in progress"}))
    r = get_status(client, db_row, fresh={"status": "in progress", "remains": 10})

    assert "В работе" in r.text
    assert 'http-equiv="refresh"' in r.text


def test_fragment_awaiting_balance_hidden_from_client(client):
    # клиенту не показываем внутреннюю причину — только «В работе»
    db_row = row(supplier="fragment", supplier_order_id="task-1")
    r = get_status(client, db_row, fresh={"status": "awaiting_balance"})

    assert "В работе" in r.text
    assert "баланс" not in r.text.lower()
    assert "awaiting_balance" not in r.text


def test_fragment_success_shows_done(client):
    db_row = row(supplier="fragment", supplier_order_id="task-1")
    r = get_status(client, db_row, fresh={"status": "success"})

    assert "Завершён" in r.text


def test_fragment_failed_shows_canceled(client):
    db_row = row(supplier="fragment", supplier_order_id="task-1")
    r = get_status(client, db_row, fresh={"status": "failed"})

    assert "отмен" in r.text.lower()


def test_order_without_status_yet(client):
    r = get_status(client, row(), fresh=None)
    assert "Ожидаем запуск заказа" in r.text


def test_supplier_error_falls_back_to_saved_status(client):
    # поставщик недоступен — показываем, что было в БД, страница не падает
    db_row = row(supplier_status=json.dumps({"status": "in progress", "remains": 5}))
    r = get_status(client, db_row, fetch_error=RuntimeError("сеть"))

    assert r.status_code == 200
    assert "В работе" in r.text


def test_final_status_does_not_hit_supplier(client):
    db_row = row(supplier_status=json.dumps({"status": "completed"}))
    fetch = AsyncMock()
    with (
        patch.object(status_route.repo, "get_by_unique_code", AsyncMock(return_value=db_row)),
        patch.object(status_route, "fetch_status", fetch),
    ):
        client.get("/status?code=CODE-1")
    fetch.assert_not_awaited()


def test_fragment_order_polls_fragment_not_legacy(client):
    db_row = row(supplier="fragment", supplier_order_id="task-1")
    with (
        patch.object(status_route.repo, "get_by_unique_code", AsyncMock(return_value=db_row)),
        patch.object(status_route.repo, "update_supplier_by_ucode", AsyncMock()),
        patch.object(registry.fragment, "get_task_status", AsyncMock(return_value={"status": "processing"})) as frg,
        patch.object(registry.teateagram, "get_supplier_status", AsyncMock()) as tea,
        patch.object(registry.smm_panel, "get_supplier_status", AsyncMock()) as smm,
    ):
        client.get("/status?code=CODE-1")

    assert frg.await_count == 1
    assert tea.await_count == 0
    assert smm.await_count == 0
