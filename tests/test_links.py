import pytest

from app.services.links import (
    INVALID_TG_USERNAME_MSG,
    extract_days_and_link,
    extract_months,
    extract_username,
    extract_username_option,
    normalize_tg_link,
    resolve_fragment_kind,
    resolve_service,
)

# опции из выгрузки GGSEL: parameters_offer_2558268.csv (Stars) и _2558302.csv (Premium)
STARS_OPTIONS = [{"id": 5547537, "name": "@username", "value": "durov", "variant_id": None}]
PREMIUM_OPTIONS = [
    {"id": 5407393, "name": "Количество месяцев подписки", "value": "3 месяца", "variant_id": 30069740},
    {"id": 5547798, "name": "@username", "value": "@durov", "variant_id": None},
]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://t.me/mychannel", "https://t.me/mychannel"),
        ("t.me/mychannel", "https://t.me/mychannel"),
        ("@mychannel", "https://t.me/mychannel"),
        ("mychannel", "https://t.me/mychannel"),
        ("https://t.me/mychannel?utm=1", "https://t.me/mychannel"),
        ("https://t.me/+AbCdEf123456", "https://t.me/+AbCdEf123456"),
        ("https://t.me/joinchat/AbCdEf123456", "https://t.me/joinchat/AbCdEf123456"),
        ("https://t.me/boost/mychannel", "https://t.me/boost/mychannel"),
    ],
)
def test_normalize_tg_link_ok(raw, expected):
    link, err = normalize_tg_link(raw)
    assert err is None
    assert link == expected


@pytest.mark.parametrize("raw", [None, "", "   ", "https://t.me/", "t.me", "abc"])
def test_normalize_tg_link_invalid(raw):
    link, err = normalize_tg_link(raw)
    assert link is None
    assert err is not None


def test_normalize_tg_link_rejects_private_c_param():
    # приватные ссылки t.me/c/... поставщик принять не может
    link, err = normalize_tg_link("https://t.me/mychannel?c=123")
    assert link is None
    assert err is not None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("https://t.me/durov", "durov"),
        ("t.me/durov", "durov"),
        ("@durov", "durov"),
        ("durov", "durov"),
        ("  T.ME/Durov?x=1  ", "Durov"),
    ],
)
def test_extract_username_ok(raw, expected):
    username, err = extract_username(raw)
    assert err is None
    assert username == expected


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "",
        "https://t.me/+AbCdEf12345",  # инвайт — публичного username нет
        "https://t.me/c/12345/67",  # приватный канал
        "t.me/joinchat/AAA",  # инвайт
        "ab",  # короче 5 символов
    ],
)
def test_extract_username_invalid(raw):
    username, err = extract_username(raw)
    assert username is None
    assert err is not None


@pytest.mark.parametrize(
    "value,expected",
    [("3 месяца", 3), ("6 месяцев", 6), ("12 месяцев", 12), ("6 months", 6)],
)
def test_extract_months_from_real_options(value, expected):
    # значения из карточки GGSEL 102558303 (docs/parameters_offer_2558302.csv)
    assert extract_months([{"name": "Количество месяцев подписки", "value": value}]) == expected


@pytest.mark.parametrize(
    "options",
    [None, [], [{"name": "Ссылка", "value": "https://t.me/durov"}], "не список"],
)
def test_extract_months_absent(options):
    assert extract_months(options) is None


def test_extract_days_and_link():
    options = [
        {"name": "Ссылка", "value": "https://t.me/mychannel"},
        {"name": "Количество дней", "value": "90"},
    ]
    days, link = extract_days_and_link(options)
    assert days == 90
    assert link == "https://t.me/mychannel"


def test_extract_username_option_stars():
    assert extract_username_option(STARS_OPTIONS) == ("durov", None)


def test_extract_username_option_premium():
    assert extract_username_option(PREMIUM_OPTIONS) == ("durov", None)


def test_extract_username_option_premium_months_still_parsed():
    assert extract_months(PREMIUM_OPTIONS) == 3


@pytest.mark.parametrize("value", ["https://t.me/durov", "t.me/durov", "@durov", "durov"])
def test_extract_username_option_accepts_any_input_form(value):
    options = [{"id": 5547537, "name": "@username", "value": value, "variant_id": None}]
    assert extract_username_option(options) == ("durov", None)


@pytest.mark.parametrize(
    "options",
    [
        [],
        None,
        "не список",
        [{"id": 59069, "name": "Ссылка на канала вида https://t.me/...", "value": "https://t.me/x"}],  # опции буста
        [{"id": 5547537, "name": "@username", "value": "+79001234567"}],  # телефон вместо ника
        [{"id": 5547537, "name": "@username", "value": ""}],
        [{"id": 5547537, "name": "@username", "value": "https://t.me/+4yuWzgnVZcVlMWJi"}],  # инвайт, не username
    ],
)
def test_extract_username_option_invalid(options):
    username, err = extract_username_option(options)
    assert username is None
    assert err == INVALID_TG_USERNAME_MSG


@pytest.mark.parametrize(
    "goods_id,expected",
    [("102558269", "stars"), ("102558303", "premium"), ("102084952", None), ("5431904", None)],
)
def test_resolve_fragment_kind_ggsel(goods_id, expected):
    assert resolve_fragment_kind("ggsel", goods_id) == expected


def test_resolve_fragment_kind_plati_has_no_fragment():
    # аналогов Stars/Premium на PLATI нет
    assert resolve_fragment_kind("plati", "5558693") is None


def test_resolve_service_fixed_product():
    assert resolve_service("ggsel", "102084952", None) == "G_BOOST_90"


def test_resolve_service_range_needs_days():
    assert resolve_service("ggsel", "5431904", None) is None
    assert resolve_service("ggsel", "5431904", 30) == "G_BOOST_30"


def test_resolve_service_rounds_days_up():
    # 45 дней → ближайший больший тариф
    assert resolve_service("ggsel", "5431904", 45) == "G_BOOST_60"
