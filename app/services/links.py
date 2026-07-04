import re
from typing import Any

from app.core.config.config import config

INVALID_TG_LINK_MSG = (
    "Указана неверная ссылка на канал. Напишите в поддержку, указав ссылку для вступления в канал. " "Мы перезапустим заказ."
)

_TME_RE = re.compile(r"^(?:https?://)?(?:www\.)?t\.me/(?P<path>.+)$", re.IGNORECASE)
_USERNAME_RE = re.compile(r"^[a-zA-Z0-9_]{5,32}$")
_BAD_C_PARAM_RE = re.compile(r"[?&]c=", re.IGNORECASE)
_NUM_RE = re.compile(r"(\d+)")
_TOKEN_RE = re.compile(r"[A-Za-z0-9_\-]{8,128}")
_BOOST_RE = re.compile(r"[A-Za-z0-9_\-]{3,128}")


def normalize_tg_link(raw: Any) -> tuple[str | None, str | None]:
    """Принимает t.me/name | @name | +invite | joinchat/.. | boost/.. → (норм. ссылка, ошибка)."""
    if raw is None:
        return None, INVALID_TG_LINK_MSG
    s = str(raw).strip().replace("\\", "/").strip()
    if not s or _BAD_C_PARAM_RE.search(s):
        return None, INVALID_TG_LINK_MSG
    if s.startswith("@"):
        s = s[1:].strip()
    if s.lower() in {"t.me", "https://t.me", "http://t.me", "www.t.me", "https://www.t.me", "http://www.t.me"}:
        return None, INVALID_TG_LINK_MSG

    m = _TME_RE.match(s)
    if m:
        path = (m.group("path") or "").split("?", 1)[0].split("#", 1)[0].strip().strip("/")
        return _validate_path(path)

    if "/" not in s and _USERNAME_RE.match(s):
        return f"https://t.me/{s}", None

    # подбираем хвост, если в строке есть t.me/
    if "t.me/" in s.lower():
        tail = s[s.lower().find("t.me/") + 5 :].split("?", 1)[0].split("#", 1)[0].strip().strip("/")
        return _validate_path(tail)

    return None, INVALID_TG_LINK_MSG


def _validate_path(path: str) -> tuple[str | None, str | None]:
    if not path:
        return None, INVALID_TG_LINK_MSG
    low = path.lower()

    if path.startswith("+"):  # инвайт +hash
        token = path[1:].strip()
        return (f"https://t.me/{path}", None) if _TOKEN_RE.fullmatch(token) else (None, INVALID_TG_LINK_MSG)

    if low.startswith("joinchat/"):
        tail = path.split("/", 1)[1].strip() if "/" in path else ""
        return (f"https://t.me/{path}", None) if _TOKEN_RE.fullmatch(tail) else (None, INVALID_TG_LINK_MSG)

    if low.startswith("boost/"):
        tail = path.split("/", 1)[1].strip() if "/" in path else ""
        return (f"https://t.me/{path}", None) if _BOOST_RE.fullmatch(tail) else (None, INVALID_TG_LINK_MSG)

    if "/" not in path and _USERNAME_RE.match(path):  # username
        return f"https://t.me/{path}", None

    return None, INVALID_TG_LINK_MSG


def _parse_days(val: Any) -> int | None:
    if isinstance(val, int | float):
        return int(val)
    if isinstance(val, str):
        s = val.strip()
        if s.isdigit():
            return int(s)
        m = _NUM_RE.search(s)
        if m:
            return int(m.group(1))
    return None


def extract_days_and_link(options: Any) -> tuple[int | None, str | None]:
    """Достаёт количество дней и TG-ссылку из опций заказа (RU+EN ключи)."""
    days: int | None = None
    link: str | None = None
    if not isinstance(options, list):
        return days, link

    for opt in options:
        try:
            name = str(opt.get("name", "")).strip().lower()
            val = opt.get("value")
        except AttributeError:
            continue

        if any(
            k in name
            for k in (
                "количество дней",
                "кол-во дней",
                "срок",
                "дней",
                "дни",
                "days count",
                "days",
                "day",
                "duration",
                "period",
                "term",
            )
        ):
            parsed = _parse_days(val)
            if parsed and parsed > 0:
                days = parsed

        vstr = str(val or "").strip()
        if vstr and ("ссылка" in name or "link" in name or "t.me/" in vstr):
            link = vstr

    return days, link


def _service_for_days(days: int) -> str:
    days_to_service = config.services.days_to_service
    if days in days_to_service:
        return days_to_service[days]
    candidates = sorted(days_to_service.keys())
    greater = [d for d in candidates if d >= days]
    return days_to_service[greater[0] if greater else candidates[-1]]


def resolve_service(platform: str, goods_id: str, days: int | None) -> str | None:
    """platform: 'ggsel' | 'plati'. Возвращает service_name поставщика или None."""
    p = platform.lower().strip()
    fixed = config.services.fixed_product_to_service.get(p, {}).get(str(goods_id))
    if fixed:
        return fixed
    if str(goods_id) in config.services.range_products.get(p, set()):
        return None if days is None else _service_for_days(int(days))
    if days is not None:
        return _service_for_days(int(days))
    return None
