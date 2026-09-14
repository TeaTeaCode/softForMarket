import pytest

from app.services import links
from app.services.links import extract_days_and_link, extract_variant_ids, resolve_service

# опции — как в выгрузке GGSEL parameters_offer_3119586.csv
LINK_OPT = {"id": 7490512, "name": " Пример ссылки t.me/+abc123xyz", "value": "https://t.me/+abc123xyz", "variant_id": None}


def service_opt(value, variant_id):
    return {"id": 7490621, "name": "Услуга", "value": value, "variant_id": variant_id}


@pytest.fixture
def variant_config(monkeypatch):
    monkeypatch.setattr(
        links.config.services,
        "variant_to_service",
        {"ggsel": {"53479612": "G_SUB_3", "53479623": "G_PREM_SUB_30", "53479627": "G_PREM_SUB_180"}, "plati": {}},
    )


# ─── разбор опций ────────────────────────────────────────────────────────────


def test_variant_ids_collected():
    options = [LINK_OPT, service_opt("Премиум-подписчики - 30 дней", 53479623)]

    assert extract_variant_ids(options) == ["53479623"]  # опция ссылки без variant_id пропущена
    assert extract_days_and_link(options) == (30, "https://t.me/+abc123xyz")


def test_variant_ids_empty_without_variants():
    assert extract_variant_ids([LINK_OPT]) == []
    assert extract_variant_ids([{"name": "Услуга", "value": "текст"}]) == []
    assert extract_variant_ids(None) == []


def test_link_option_named_like_example_is_recognized():
    # имя опции «Пример ссылки …» — ссылка берётся даже без t.me/ в значении
    _, link = extract_days_and_link([{"name": " Пример ссылки t.me/+abc123xyz", "value": "@mychannel"}])
    assert link == "@mychannel"


# ─── resolve_service ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "variant_id,expected",
    [(53479612, "G_SUB_3"), (53479623, "G_PREM_SUB_30"), (53479627, "G_PREM_SUB_180")],
)
def test_service_resolved_by_variant(variant_config, variant_id, expected):
    ids = extract_variant_ids([LINK_OPT, service_opt("любой текст", variant_id)])

    assert resolve_service("ggsel", "103119587", None, ids) == expected


def test_service_resolved_regardless_of_variant_title(variant_config):
    # переименование варианта в GGSEL не меняет маршрут
    ids = extract_variant_ids([service_opt("Совсем другое название", 53479623)])

    assert resolve_service("ggsel", "103119587", None, ids) == "G_PREM_SUB_30"


def test_unknown_variant_is_rejected_loudly(variant_config, monkeypatch):
    monkeypatch.setattr(links.config.services, "range_products", {"ggsel": set(), "plati": set()})
    monkeypatch.setattr(links.config.services, "fixed_product_to_service", {"ggsel": {}, "plati": {}})
    # новый вариант, которого нет в конфиге → заказ уходит в ошибку, а не в чужой сервис
    assert resolve_service("ggsel", "103119587", None, ["99999999"]) is None


def test_variants_are_platform_scoped(variant_config, monkeypatch):
    monkeypatch.setattr(links.config.services, "range_products", {"ggsel": set(), "plati": set()})
    monkeypatch.setattr(links.config.services, "fixed_product_to_service", {"ggsel": {}, "plati": {}})
    monkeypatch.setattr(links.config.services, "days_to_service", {30: "G_BOOST_30"})
    # тот же id на PLATI не известен → общий fallback по дням
    assert resolve_service("plati", "103119587", 30, ["53479623"]) == "G_BOOST_30"


# ─── бусты: вариант известен, но в конфиге его нет → прежний путь по дням ────


@pytest.mark.parametrize(
    "variant_id,expected",
    [
        (33095693, "G_BOOST_1"),
        (33095694, "G_BOOST_7"),
        (33095695, "G_BOOST_30"),
        (33095867, "G_BOOST_60"),
        (47125218, "G_BOOST_90"),
    ],
)
def test_boost_resolved_by_variant(monkeypatch, variant_id, expected):
    monkeypatch.setattr(
        links.config.services,
        "variant_to_service",
        {
            "ggsel": {
                "33095693": "G_BOOST_1",
                "33095694": "G_BOOST_7",
                "33095695": "G_BOOST_30",
                "33095867": "G_BOOST_60",
                "47125218": "G_BOOST_90",
            },
            "plati": {},
        },
    )
    # срок в тексте намеренно не совпадает: маршрут задаёт id, а не значение
    options = [{"id": 5804796, "name": "Количество дней", "value": "чушь", "variant_id": variant_id}]

    assert resolve_service("ggsel", "5431904", None, extract_variant_ids(options)) == expected


def test_boost_falls_back_to_days(variant_config, monkeypatch):
    monkeypatch.setattr(links.config.services, "range_products", {"ggsel": {"5431904"}, "plati": set()})
    monkeypatch.setattr(links.config.services, "days_to_service", {7: "G_BOOST_7", 90: "G_BOOST_90"})
    # заказ из лога: variant_id=47125218 «90 дней», в variant_to_service его нет
    options = [
        {"id": 59069, "name": "Ссылка на канала вида https://t.me/...", "value": "https://t.me/ch", "variant_id": None},
        {"id": 5804796, "name": "Количество дней", "value": "90 дней", "variant_id": 47125218},
    ]
    days, _ = extract_days_and_link(options)

    assert resolve_service("ggsel", "5431904", days, extract_variant_ids(options)) == "G_BOOST_90"


def test_fixed_product_still_wins_without_variant(variant_config, monkeypatch):
    monkeypatch.setattr(links.config.services, "fixed_product_to_service", {"ggsel": {"102084952": "G_BOOST_90"}})

    assert resolve_service("ggsel", "102084952", None, []) == "G_BOOST_90"
