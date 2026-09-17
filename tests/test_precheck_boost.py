import json

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
import pytest

from app.api.v1.routes.ggsel import ONE_DAY_VARIANT_ID, router

LINK_OPT = {"id": 59069, "type": "text", "value": "https://t.me/boost/channel"}
DAYS_30_VARIANT = 33095695


def body(cnt, variant_id=ONE_DAY_VARIANT_ID):
    return {
        "product": {"id": 5431904, "cnt": cnt, "lang": "ru-RU"},
        "options": [LINK_OPT, {"id": 5804796, "type": "radio", "value": variant_id}],
    }


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


# ─── отказ: 1 день сверх лимита ──────────────────────────────────────────────


@pytest.mark.parametrize("cnt", [10.2, 11.0, 15.0, 94.0, 344.83])
async def test_one_day_over_limit_rejected(client, cnt):
    r = await client.post("/ggsel/precheck/boost", json=body(cnt))

    assert r.status_code == 400
    assert r.json() == {"result": "reject"}  # причину покупателю не раскрываем


# ─── разрешено ───────────────────────────────────────────────────────────────


@pytest.mark.parametrize("cnt", [1.0, 2.0, 8.62, 10.0])
async def test_one_day_within_limit_allowed(client, cnt):
    r = await client.post("/ggsel/precheck/boost", json=body(cnt))

    assert r.status_code == 200
    assert r.json() == {"result": "ok"}


@pytest.mark.parametrize("cnt", [11.0, 60.0, 300.0])
async def test_other_terms_not_limited(client, cnt):
    # лимит только на 1 день — 30-дневные бусты любого объёма проходят
    r = await client.post("/ggsel/precheck/boost", json=body(cnt, variant_id=DAYS_30_VARIANT))

    assert r.status_code == 200


# ─── некорректные тела не блокируют покупку ─────────────────────────────────


@pytest.mark.parametrize(
    "payload",
    ["", "не json", "{}", '{"product":{}}', '{"product":{"cnt":"много"},"options":[]}'],
)
async def test_unparsable_body_is_allowed(client, payload):
    r = await client.post("/ggsel/precheck/boost", content=payload, headers={"content-type": "application/json"})

    assert r.status_code == 200


async def test_get_without_body_is_allowed(client):
    r = await client.get("/ggsel/precheck/boost")

    assert r.status_code == 200


async def test_both_paths_behave_the_same(client):
    over = body(50.0)
    assert (await client.post("/ggsel/precheck/boost", json=over)).status_code == 400
    assert (await client.post("/api/v1/ggsel/precheck/boost", json=over)).status_code == 400


# ─── регрессия на реальных телах из лога ────────────────────────────────────


REAL_BODIES = [
    (
        '{"product":{"id":5431904,"cnt":8.0,"lang":"ru-RU"},"options":[{"id":59069,"type":"text","value":"https://t.me/boost/FanCharliexanax"},{"id":5804796,"type":"radio","value":47125218}]}',
        200,
    ),
    (
        '{"product":{"id":5431904,"cnt":2.0,"lang":"ru-RU"},"options":[{"id":59069,"type":"text","value":"https://t.me/halseyla"},{"id":5804796,"type":"radio","value":33095867}]}',
        200,
    ),
    (
        '{"product":{"id":5431904,"cnt":344.83,"lang":"ru-RU"},"options":[{"id":59069,"type":"text","value":"https://t.me/x"},{"id":5804796,"type":"radio","value":33095693}]}',
        400,
    ),
    (
        '{"product":{"id":5431904,"cnt":10.0,"lang":"ru-RU"},"options":[{"id":59069,"type":"text","value":"https://t.me/x"},{"id":5804796,"type":"radio","value":33095693}]}',
        200,
    ),
]


@pytest.mark.parametrize("raw,expected", REAL_BODIES)
async def test_real_log_bodies(client, raw, expected):
    r = await client.post("/ggsel/precheck/boost", content=raw, headers={"content-type": "application/json"})

    assert r.status_code == expected, f"{json.loads(raw)['product']['cnt']} → {r.status_code}"
