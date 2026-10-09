import pytest

from app.api.v1.routes.status import _human_status, _is_final, _service_label

# Статусы Fragment: pending | processing | awaiting_balance | success | failed


@pytest.mark.parametrize(
    "status,expected",
    [
        ("success", "Завершён"),
        ("pending", "В работе"),
        ("processing", "В работе"),
        ("awaiting_balance", "В работе"),
    ],
)
def test_fragment_status_is_human_readable(status, expected):
    line, _ = _human_status({"status": status})
    assert line == expected


def test_fragment_failed_shown_as_canceled():
    line, remains = _human_status({"status": "failed"})
    assert "отмен" in line.lower()
    assert remains == 0


@pytest.mark.parametrize(
    "status,expected",
    [("success", True), ("failed", True), ("pending", False), ("processing", False), ("awaiting_balance", False)],
)
def test_is_final_for_fragment(status, expected):
    assert _is_final({"status": status}) is expected


# Статусы SMM Panel


@pytest.mark.parametrize(
    "status,expected",
    [("completed", "Завершён"), ("in progress", "В работе"), ("partial", "Частично выполнен")],
)
def test_smm_panel_status_is_human_readable(status, expected):
    line, _ = _human_status({"status": status})
    assert line == expected


def test_paused_is_not_final():
    assert _is_final({"status": "paused"}) is False


def test_remains_extracted_from_supplier_response():
    _, remains = _human_status({"status": "in progress", "remains": "42"})
    assert remains == 42


def test_unknown_status_passed_through_to_client():
    line, _ = _human_status({"status": "какой-то новый статус"})
    assert line == "какой-то новый статус"


@pytest.mark.parametrize(
    "service,expected",
    [
        ("G_BOOST_30", "Буст"),
        ("G_SUB_7", "Подписчики"),
        ("G_PREM_SUB_180", "Премиум подписчики"),
    ],
)
def test_service_label_by_service_name(service, expected):
    assert _service_label({"service": service, "goods_id": "103119587"}) == expected


def test_service_label_falls_back_to_goods_name_for_old_orders():
    assert _service_label({"service": None, "goods_id": "unknown"}) == "unknown"
