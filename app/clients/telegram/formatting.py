import re
from typing import Any

from app.core.config.config import config

_TZ_SUFFIX_RE = re.compile(r"(?:Z|[+\-]\d{2}:?\d{2})$")

DONE_HEADER = "✅ ЗАКАЗ ВЫПОЛНЕН"


def html_escape(s: Any) -> str:
    if s is None:
        return ""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_money(amount: float | None, curr: str | None) -> str:
    if amount is None:
        return "—"
    try:
        a = float(amount)
    except (TypeError, ValueError):
        return "—"
    code = (curr or "").upper()
    return f"{a:.2f} {code}".strip()


def footer_for_platform(platform: str) -> str:
    p = (platform or "").strip().lower()
    return config.services.footer_by_platform.get(p, "")


def options_to_lines(options: list) -> str:
    lines = []
    for opt in options or []:
        name = str(opt.get("name") or "").strip()
        value = str(opt.get("value") or opt.get("user_data") or "").strip()
        if name or value:
            lines.append(f"— {html_escape(name)}: {html_escape(value)}")
    return "\n".join(lines)


def supplier_canceled(status_obj: Any) -> bool:
    """Признак отмены/возврата заказа (терпимо к разным форматам ответа)."""
    try:
        if isinstance(status_obj, str):
            s = status_obj.lower()
            return "cancel" in s or "refun" in s or '"error": true' in s
        if isinstance(status_obj, dict):
            st = str(status_obj.get("status", "")).lower()
            if st in {"canceled", "cancelled", "refunded", "refund", "error", "failed"}:
                return True
            if bool(status_obj.get("error")):
                return True
            state = str(status_obj.get("state", "")).lower()
            if state in {"cancel", "canceled", "refunded"}:
                return True
            if "order" in status_obj and isinstance(status_obj["order"], dict):
                return supplier_canceled(status_obj["order"])
    except (AttributeError, TypeError):
        return False
    return False


def status_header_ru(status_json: dict[str, Any] | None) -> str:
    if not status_json:
        return "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"
    status_raw = str(status_json.get("status", "")).strip()
    status = status_raw.lower()
    is_error = bool(status_json.get("error")) or "error" in status
    if status in {"canceled", "cancelled", "refunded", "refund", "failed"} or is_error or supplier_canceled(status_json):
        return "❌ ЗАКАЗ ОТМЕНЁН"
    if status in {"completed", "success", "done", "finished"}:
        return DONE_HEADER
    if status in {"partial"}:
        return "⚠️ ЗАКАЗ ВЫПОЛНЕН ЧАСТИЧНО"
    if status in {"paused"}:
        return "⏸️ ЗАКАЗ ПРИОСТАНОВЛЕН"
    if status in {"awaiting_balance"}:
        return "💰 ОЖИДАЕТ ПОПОЛНЕНИЯ БАЛАНСА"
    if status in {"pending", "inprogress", "in progress", "processing", "progress", "working"} or not status:
        return "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"
    return f"ℹ️ СТАТУС: {status_raw or 'Неизвестен'}"


def is_done(status_json: Any) -> bool:
    """Заказ выполнен полностью — об этом в TG не уведомляем."""
    return isinstance(status_json, dict) and status_header_ru(status_json) == DONE_HEADER


# ─── общий каркас уведомлений ────────────────────────────────────────────────
# <b>ЗАГОЛОВОК</b> · ПЛОЩАДКА
#
# <b>Поле:</b> значение   (пустые поля пропускаются)
# <b>Параметры:</b> опции заказа
# <b>Проверка статуса:</b> ссылка

FAIL_HEADER = "❌ ЗАКАЗ НЕ ОФОРМЛЕН"
CHAT_HEADER = "💬 НОВОЕ СООБЩЕНИЕ"


def _code(value: Any) -> str | None:
    s = str(value or "").strip()
    return f"<code>{html_escape(s)}</code>" if s and s != "—" else None


def _goods(name: Any, goods_id: Any) -> str | None:
    name_s, gid = (str(x or "").strip().replace("—", "") for x in (name, goods_id))
    if gid in ("", name_s):
        return html_escape(name_s or gid) or None
    return f"{html_escape(name_s or '—')} (goods_id={html_escape(gid)})"


def _payment(p: dict[str, Any]) -> str | None:
    currency = str(p.get("type_curr") or p.get("currency_type") or "")
    amount = fmt_money(p.get("amount"), currency)
    if amount == "—":
        return None
    parts = [amount]
    if p.get("profit") is not None:
        parts.append(f"выплата {fmt_money(p.get('profit'), currency)}")
    if p.get("amount_usd") is not None:
        parts.append(fmt_money(p.get("amount_usd"), "USD"))
    return " · ".join(parts)


def _compose(
    header: str, platform: str, fields: list[tuple[str, str | None]], options: list | None, status_url: str | None
) -> str:
    lines = [f"<b>{html_escape(header)}</b> · {html_escape(platform)}", ""]
    lines += [f"<b>{label}:</b> {value}" for label, value in fields if value]
    if opts := options_to_lines(options or []):
        lines += ["<b>Параметры:</b>", opts]
    if status_url:
        lines.append(f'<b>Проверка статуса:</b> <a href="{status_url}">{status_url}</a>')
    return "\n".join(lines)


def _order_fields(p: dict[str, Any], unique_code: str, email: str, goods_name: str) -> list[tuple[str, str | None]]:
    return [
        ("Товар", _goods(goods_name, p.get("id_goods") or p.get("goods_id"))),
        ("Invoice", _code(p.get("inv"))),
        ("UniqueCode", _code(unique_code)),
        ("Покупатель", html_escape(email).strip() or None),
        ("Оплата", _payment(p)),
        ("Количество", html_escape(p.get("cnt_goods")).strip() or None),
    ]


def fmt_unified_order_msg(
    platform_name: str,
    unique_code: str,
    p: dict[str, Any],
    email: str,
    goods_name: str,
    options: list,
    supplier_resp: dict[str, Any],
    status_info: dict[str, Any] | None,
    status_url: str,
) -> str:
    order_id = supplier_resp.get("order") or supplier_resp.get("order_id") or supplier_resp.get("id")
    fields = [
        *_order_fields(p, unique_code, email, goods_name),
        ("Заказ поставщика", _code(order_id)),
    ]
    return _compose(status_header_ru(status_info), platform_name, fields, options, status_url)


def fmt_failure_msg(
    platform_name: str,
    unique_code: str,
    p: dict[str, Any],
    email: str,
    goods_name: str,
    options: list,
    error_text: str,
    status_url: str,
    *,
    link: str | None = None,
    days: int | None = None,
    quantity: int | None = None,
    service_id: str | int | None = None,
) -> str:
    fields = [("Ошибка", html_escape(error_text).strip() or None)]
    fields += _order_fields(p | {"cnt_goods": quantity}, unique_code, email, goods_name)
    fields += [
        ("Ссылка", html_escape(link).strip() if link and link != "—" else None),
        ("Дней", None if days is None else str(days)),
        ("Услуга", _code(service_id)),
    ]
    return _compose(FAIL_HEADER, platform_name, fields, options, status_url)


def _clean_date(s: str) -> str:
    """ISO-дата в читаемый вид: T→пробел, без tz-суффикса."""
    if not s:
        return ""
    return _TZ_SUFFIX_RE.sub("", s.replace("T", " ")).strip()


def fmt_chat_msg_for_tg(chat: dict[str, Any], msg: dict[str, Any], id_i: int) -> str:
    """Форматирование пересылаемого сообщения из чата GGSEL."""
    buyer_email = chat.get("email") or chat.get("buyer_email") or chat.get("buyer") or ""
    buyer_name = chat.get("name") or chat.get("buyer_name") or ""
    buyer = " ".join(str(x).strip() for x in (buyer_name, buyer_email) if x and str(x).strip() != "—")

    filename = msg.get("filename") or ""
    url = msg.get("url") or ""
    attachment = None
    if msg.get("is_img"):
        attachment = ("Изображение", html_escape(url or msg.get("preview") or "") or "—")
    elif msg.get("is_file"):
        attachment = ("Файл", f"{html_escape(filename) or '—'} — {html_escape(url) or '—'}")

    fields = [
        ("Покупатель", html_escape(buyer) or None),
        ("Товар", _goods(chat.get("name_goods") or chat.get("goods_name"), chat.get("id_goods") or chat.get("goods_id"))),
        ("Чат", _code(id_i)),
        ("Дата", html_escape(_clean_date(str(msg.get("date_written") or ""))) or None),
    ]
    if attachment:
        fields.append(attachment)

    text = _compose(CHAT_HEADER, "GGSEL", fields, None, None)
    body = str(msg.get("message") or "")
    if body:
        # обрезаем под лимит Telegram (4096)
        body = body if len(body) <= 3500 else body[:3500] + "…"
        text += f"\n<b>Текст:</b>\n{html_escape(body)}"
    return text
