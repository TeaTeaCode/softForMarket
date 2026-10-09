from app.clients.telegram.formatting import fmt_chat_msg_for_tg, fmt_failure_msg, fmt_unified_order_msg

PURCHASE = {
    "inv": 777,
    "id_goods": "6218292",
    "amount": 25.5,
    "profit": 22,
    "amount_usd": 0.31,
    "type_curr": "RUB",
    "cnt_goods": "500",
}
OPTIONS = [{"name": "Ссылка", "value": "https://t.me/+abc"}, {"name": "Услуга", "value": "Обычные подписчики - 3 дня"}]
URL = "https://example.com/status?code=CODE"


def order_msg(status=None, **over):
    args = {
        "platform_name": "PLATI",
        "unique_code": "CODE",
        "p": PURCHASE,
        "email": "b@x.ru",
        "goods_name": "Подписчики Telegram",
        "options": OPTIONS,
        "supplier_resp": {"order": "417"},
        "status_info": status,
        "status_url": URL,
    }
    return fmt_unified_order_msg(**(args | over))


def test_order_message_layout():
    assert order_msg({"status": "in progress", "total_count": 500, "completed_count": 100}) == "\n".join(
        [
            "<b>👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ</b> · PLATI",
            "",
            "<b>Товар:</b> Подписчики Telegram (goods_id=6218292)",
            "<b>Invoice:</b> <code>777</code>",
            "<b>UniqueCode:</b> <code>CODE</code>",
            "<b>Покупатель:</b> b@x.ru",
            "<b>Оплата:</b> 25.50 RUB · выплата 22.00 RUB · 0.31 USD",
            "<b>Количество:</b> 500",
            "<b>Заказ поставщика:</b> <code>417</code>",
            "<b>Статус поставщика:</b> in progress · выполнено 100 из 500",
            "<b>Параметры:</b>",
            "— Ссылка: https://t.me/+abc",
            "— Услуга: Обычные подписчики - 3 дня",
            f'<a href="{URL}">Страница статуса</a>',
        ]
    )


def test_failure_message_shares_layout_and_leads_with_error():
    msg = fmt_failure_msg(
        "GGSEL", "CODE", PURCHASE, "b@x.ru", "Подписчики Telegram", OPTIONS, "нет service_id", URL, quantity=500
    )
    lines = msg.split("\n")

    assert lines[:3] == ["<b>❌ ЗАКАЗ НЕ ОФОРМЛЕН</b> · GGSEL", "", "<b>Ошибка:</b> нет service_id"]
    # остальные поля — те же, что в уведомлении о статусе
    order_lines = order_msg().split("\n")
    for line in ("<b>Товар:</b>", "<b>Invoice:</b>", "<b>UniqueCode:</b>", "<b>Оплата:</b>", "<b>Количество:</b>"):
        assert next(x for x in lines if x.startswith(line)) == next(x for x in order_lines if x.startswith(line))
    assert lines[-1] == f'<a href="{URL}">Страница статуса</a>'


def test_empty_fields_are_skipped():
    # исключение до разбора заказа: известны только площадка, код и ошибка
    msg = fmt_failure_msg("PLATI", "CODE", {}, "", "—", [], "сеть", URL)

    assert msg.split("\n") == [
        "<b>❌ ЗАКАЗ НЕ ОФОРМЛЕН</b> · PLATI",
        "",
        "<b>Ошибка:</b> сеть",
        "<b>UniqueCode:</b> <code>CODE</code>",
        f'<a href="{URL}">Страница статуса</a>',
    ]


def test_goods_id_not_repeated_when_name_unknown():
    # goods_human не знает товар → название = id
    assert "<b>Товар:</b> 6218292\n" in order_msg(goods_name="6218292")


def test_user_text_is_escaped():
    # «<» в ошибке или опциях ломал parse_mode=HTML, и Telegram отклонял сообщение
    msg = fmt_failure_msg(
        "GGSEL", "CODE", {}, "", "Товар <b>", [{"name": "Ссылка", "value": "<script>"}], "HTTP 500 <html>", URL
    )

    assert "<b>Ошибка:</b> HTTP 500 &lt;html&gt;" in msg
    assert "— Ссылка: &lt;script&gt;" in msg
    assert "<b>Товар:</b> Товар &lt;b&gt;" in msg


def test_chat_message_shares_layout():
    chat = {"email": "b@x.ru", "name_goods": "Подписчики Telegram", "id_goods": "103119587"}
    msg = {"message": "где заказ?", "date_written": "2026-10-09T12:00:00+03:00"}

    assert fmt_chat_msg_for_tg(chat, msg, 42).split("\n") == [
        "<b>💬 НОВОЕ СООБЩЕНИЕ</b> · GGSEL",
        "",
        "<b>Покупатель:</b> b@x.ru",
        "<b>Товар:</b> Подписчики Telegram (goods_id=103119587)",
        "<b>Чат:</b> <code>42</code>",
        "<b>Дата:</b> 2026-10-09 12:00:00",
        "<b>Текст:</b>",
        "где заказ?",
    ]
