import json

import httpx
import pytest

from app.clients.suppliers.fragment import PREMIUM_MONTHS, FragmentApi, Source
from app.clients.suppliers.smm_panel import SmmPanelApi


def _api(cls, handler, base_url="https://supplier.test"):
    api = cls()
    api._client = httpx.AsyncClient(
        base_url=base_url,
        headers={"X-API-Key": "secret"},
        transport=httpx.MockTransport(handler),
    )
    return api


# ─── Fragment ────────────────────────────────────────────────────────────────


async def test_fragment_stars_sends_contract_and_returns_task_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        seen["key"] = request.headers.get("x-api-key")
        return httpx.Response(200, json={"task_id": "task-42"})

    api = _api(FragmentApi, handler)
    task_id = await api.create_stars_order("durov", 50, Source.ggsel)

    assert task_id == "task-42"
    assert seen["path"] == "/api/purchase/stars"
    assert seen["key"] == "secret"
    assert seen["body"] == {"username": "durov", "quantity": 50, "source": "ggsel"}


async def test_fragment_premium_sends_contract_and_returns_task_id():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"task_id": "task-77"})

    api = _api(FragmentApi, handler)
    task_id = await api.create_premium_order("durov", 6, Source.digiseller)

    assert task_id == "task-77"
    assert seen["path"] == "/api/purchase/premium"
    assert seen["body"] == {"username": "durov", "months": 6, "source": "digiseller"}


@pytest.mark.parametrize("months", sorted(PREMIUM_MONTHS))
async def test_fragment_premium_accepts_allowed_months(months):
    api = _api(FragmentApi, lambda r: httpx.Response(200, json={"task_id": "t"}))
    assert await api.create_premium_order("durov", months, Source.ggsel) == "t"


@pytest.mark.parametrize("months", [0, 1, 2, 5, 13, 24])
async def test_fragment_premium_rejects_other_months_before_request(months):
    # Fragment валидирует months ∈ {3,6,12} и вернул бы 422 — отбиваем до запроса
    called = False

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal called
        called = True
        return httpx.Response(200, json={"task_id": "t"})

    api = _api(FragmentApi, handler)
    with pytest.raises(RuntimeError, match="только на"):
        await api.create_premium_order("durov", months, Source.ggsel)
    assert called is False


async def test_fragment_raises_without_task_id():
    api = _api(FragmentApi, lambda r: httpx.Response(200, json={"detail": "oops"}))
    with pytest.raises(RuntimeError, match="task_id"):
        await api.create_stars_order("durov", 10, Source.ggsel)


async def test_fragment_task_status():
    api = _api(FragmentApi, lambda r: httpx.Response(200, json={"status": "processing", "error_text": None}))
    assert await api.get_task_status("task-1") == {"status": "processing", "error_text": None}


async def test_fragment_task_status_404_raises():
    # FragmentSoft отдаёт 404, если задачи нет
    api = _api(FragmentApi, lambda r: httpx.Response(404, json={"detail": "Задача не найдена"}))
    with pytest.raises(httpx.HTTPStatusError):
        await api.get_task_status("no-such")


@pytest.mark.parametrize("payload,expected", [(True, True), (False, False)])
async def test_fragment_check_username(payload, expected):
    api = _api(FragmentApi, lambda r: httpx.Response(200, json=payload))
    assert await api.check_username("durov") is expected


# ─── SMM Panel ───────────────────────────────────────────────────────────────


async def test_smm_panel_maps_id_to_order_for_compatibility():
    api = _api(SmmPanelApi, lambda r: httpx.Response(200, json={"id": "abc-1"}))
    data = await api.create_supplier_order("G_BOOST_90", "https://t.me/chan", 1)
    # код заказа читает "order" — формат унаследован от TeaTeaGram
    assert data["order"] == "abc-1"
    assert data["id"] == "abc-1"


async def test_smm_panel_raises_without_id():
    api = _api(SmmPanelApi, lambda r: httpx.Response(200, json={"error": "no"}))
    with pytest.raises(RuntimeError, match="не принял заказ"):
        await api.create_supplier_order("G_BOOST_90", "https://t.me/chan", 1)
