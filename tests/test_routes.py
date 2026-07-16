from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient
import pytest

from app.api.v1.routes import digiseller as digi_route, ggsel as ggsel_route
from app.db.session import get_session
from app.main import app


@pytest.fixture
def client():
    async def fake_session():
        yield None

    app.dependency_overrides[get_session] = fake_session
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() is True


def test_ggsel_requires_code(client):
    r = client.get("/ggsel")
    assert r.status_code == 400


def test_digiseller_requires_code(client):
    r = client.get("/digiseller-callback")
    assert r.status_code == 400


def test_ggsel_redirects_to_status(client):
    with patch.object(ggsel_route, "process_ggsel", AsyncMock(return_value="ABC")) as proc:
        r = client.get("/ggsel?uniquecode=ABC", follow_redirects=False)

    assert r.status_code == 302
    assert "/status?code=ABC" in r.headers["location"]
    proc.assert_awaited_once()


def test_ggsel_accepts_unique_code_alias(client):
    with patch.object(ggsel_route, "process_ggsel", AsyncMock(return_value="ABC")) as proc:
        r = client.get("/ggsel?unique_code=ABC", follow_redirects=False)

    assert r.status_code == 302
    proc.assert_awaited_once()


@pytest.mark.parametrize("path", ["/stars", "/premium"])
def test_fragment_routes_redirect_to_status(client, path):
    with patch.object(ggsel_route, "process_ggsel", AsyncMock(return_value="ABC")) as proc:
        r = client.get(f"{path}?uniquecode=ABC", follow_redirects=False)

    assert r.status_code == 302
    assert "/status?code=ABC" in r.headers["location"]
    proc.assert_awaited_once()


@pytest.mark.parametrize("path", ["/stars", "/premium"])
def test_fragment_routes_require_code(client, path):
    assert client.get(path).status_code == 400


@pytest.mark.parametrize("path", ["/stars", "/premium"])
def test_fragment_routes_accept_unique_code_alias(client, path):
    with patch.object(ggsel_route, "process_ggsel", AsyncMock(return_value="ABC")) as proc:
        r = client.get(f"{path}?unique_code=ABC", follow_redirects=False)

    assert r.status_code == 302
    proc.assert_awaited_once()


def test_digiseller_redirects_to_status(client):
    with patch.object(digi_route, "process_digiseller", AsyncMock(return_value="XYZ")) as proc:
        r = client.get("/digiseller-callback?uniquecode=XYZ", follow_redirects=False)

    assert r.status_code == 302
    assert "/status?code=XYZ" in r.headers["location"]
    proc.assert_awaited_once()


def test_ggsel_code_is_trimmed(client):
    with patch.object(ggsel_route, "process_ggsel", AsyncMock(return_value="ABC")) as proc:
        client.get("/ggsel?uniquecode=%20ABC%20", follow_redirects=False)

    assert proc.await_args.args[1] == "ABC"


def test_index_route_removed(client):
    # текстовая справка удалена — её роль выполняет /openapi
    assert client.get("/").status_code == 404


def test_api_v1_prefix_is_gone(client):
    # префикс убран коммитом 8b5d663 — старые пути не должны отвечать
    assert client.get("/api/v1/health").status_code == 404
    assert client.get("/api/v1/ggsel?uniquecode=A", follow_redirects=False).status_code == 404
