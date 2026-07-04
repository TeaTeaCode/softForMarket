import re
from typing import Any

from app.core.config.config import config

_TZ_SUFFIX_RE = re.compile(r"(?:Z|[+\-]\d{2}:?\d{2})$")


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
        name = str(opt.get("name") or "")
        value = str(opt.get("value") or opt.get("user_data") or "")
        if name or value:
            lines.append(f"— {name}: {value}")
    return "\n".join(lines) if lines else "—"


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
        return "✅ ЗАКАЗ ВЫПОЛНЕН"
    if status in {"partial"}:
        return "⚠️ ЗАКАЗ ВЫПОЛНЕН ЧАСТИЧНО"
    if status in {"paused"}:
        return "⏸️ ЗАКАЗ ПРИОСТАНОВЛЕН"
    if status in {"pending", "inprogress", "in progress", "processing", "progress", "working"} or not status:
        return "👌 ЗАКАЗ В ПРОЦЕССЕ ВЫПОЛНЕНИЯ"
    return f"ℹ️ СТАТУС: {status_raw or 'Неизвестен'}"


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
    header = status_header_ru(status_info)
    opts_block = options_to_lines(options)
    order_id = supplier_resp.get("order") or supplier_resp.get("order_id") or supplier_resp.get("id") or "—"
    amount_usd = p.get("amount_usd")
    profit = p.get("profit")
    currency = str(p.get("type_curr") or p.get("currency_type") or "")
    inv = p.get("inv", "—")
    goods_id = str(p.get("id_goods") or p.get("goods_id") or "—")

    line_money = f"• <b>Оплата {platform_name}</b>: {fmt_money(p.get('amount'), currency)}"
    if profit is not None:
        line_money += f" | <b>Выплата</b>: {fmt_money(profit, currency)}"
    if amount_usd is not None:
        line_money += f" ({fmt_money(amount_usd, 'USD')})"

    lines = [
        header,
        f"ℹ️ <b>{platform_name} → заказ оформлен</b>",
        f"<b>Товар:</b> {goods_name} (goods_id={goods_id})",
        f"<b>Invoice</b>: {inv}",
        f"<b>UniqueCode:</b> <code>{unique_code}</code>",
        line_money,
        f"<b>ID заказа у поставщика:</b> <code>{order_id}</code>",
        f'<b>Проверка статуса:</b> <a href="{status_url}">{status_url}</a>',
        f"<b>Подробности:</b>\n{opts_block}",
    ]

    if status_info:
        st = str(status_info.get("status", "")).strip()
        ch = str(status_info.get("charge", "")).strip()
        rm = str(status_info.get("remains", "")).strip()
        sc = str(status_info.get("start_count", "")).strip()
        cur = str(status_info.get("currency", "")).strip()
        lines.append(f"<b>Статус поставщика:</b> {st} | charge={ch} {cur} | remains={rm} | start={sc}")

    return "\n".join(lines) + "\n"


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
    opts_block = options_to_lines(options)
    currency = str(p.get("type_curr") or p.get("currency_type") or "")
    qty = "—" if quantity is None else str(quantity)

    lines = [
        f"❌ <b>Не удалось оформить заказ у поставщика</b> ({platform_name})",
        f"<b>Товар:</b> {goods_name} <i>(goods_id={p.get('id_goods') or p.get('goods_id') or '—'})</i>",
        f"<b>Invoice:</b> {p.get('inv', '—')!s}",
        f"<b>UniqueCode:</b> <code>{unique_code}</code>",
        f"<b>Сумма:</b> {p.get('amount') or '—'!s} {currency}",
        f"<b>Количество (cnt_goods):</b> {qty}",
        f"<b>Email покупателя:</b> {email or '—'}",
        f"<b>Ошибка:</b> <code>{error_text}</code>",
        f'<b>Проверка статуса:</b> <a href="{status_url}">{status_url}</a>',
        f"<b>Подробности (options):</b>\n{opts_block}",
    ]

    extra = []
    if link:
        extra.append(f"<b>Ссылка:</b> {link}")
    if days is not None:
        extra.append(f"<b>Дней:</b> {days!s}")
    if service_id is not None:
        extra.append(f"<b>service_id:</b> {service_id!s}")
    if extra:
        lines.append("\n".join(extra))

    return "\n".join(lines) + "\n"


def _clean_date(s: str) -> str:
    """'2026-04-20T01:48:17+03:00' → '2026-04-20 01:48:17' (T→пробел, без tz-суффикса)."""
    if not s:
        return ""
    return _TZ_SUFFIX_RE.sub("", s.replace("T", " ")).strip()


def fmt_chat_msg_for_tg(chat: dict[str, Any], msg: dict[str, Any], id_i: int) -> str:
    """Форматирование пересылаемого сообщения из чата GGSEL."""
    buyer_email = chat.get("email") or chat.get("buyer_email") or chat.get("buyer") or "—"
    buyer_name = chat.get("name") or chat.get("buyer_name") or ""
    goods_id = chat.get("id_goods") or chat.get("goods_id") or chat.get("item_id") or ""
    goods_name = chat.get("name_goods") or chat.get("goods_name") or ""

    text = str(msg.get("message") or "")
    date_w = _clean_date(str(msg.get("date_written") or ""))
    is_file = bool(msg.get("is_file"))
    is_img = bool(msg.get("is_img"))
    filename = msg.get("filename") or ""
    url = msg.get("url") or ""
    preview = msg.get("preview") or ""

    from_line = html_escape(buyer_name).strip()
    if buyer_email and str(buyer_email) != "—":
        from_line = (from_line + " " if from_line else "") + html_escape(buyer_email)
    if not from_line:
        from_line = "—"

    parts = [
        "💬 <b>Новое сообщение GGSEL</b>",
        f"<b>От:</b> {from_line}",
    ]
    if goods_id or goods_name:
        parts.append(f"<b>Товар:</b> {html_escape(goods_name or '—')} (goods_id={html_escape(goods_id or '—')})")
    parts.append(f"<b>Чат:</b> id_i=<code>{id_i}</code>")
    if date_w:
        parts.append(f"<b>Дата:</b> {html_escape(date_w)}")

    if is_img:
        parts.append(f"🖼 <b>Изображение:</b> {html_escape(url or preview) or '—'}")
    elif is_file:
        parts.append(f"📎 <b>Файл:</b> {html_escape(filename) or '—'} — {html_escape(url) or '—'}")

    if text:
        # обрезаем под лимит Telegram (4096)
        t = text if len(text) <= 3500 else text[:3500] + "…"
        parts.append(f"<b>Текст:</b>\n{html_escape(t)}")

    return "\n".join(parts)
