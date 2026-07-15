import json
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request
from fastapi.responses import PlainTextResponse, Response
from fastapi.templating import Jinja2Templates
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.clients.suppliers.registry import fetch_status
from app.clients.telegram.formatting import footer_for_platform, supplier_canceled
from app.core.config.config import config
from app.db import repository as repo
from app.db.session import get_session
from app.services.links import INVALID_TG_LINK_MSG

router = APIRouter()
_templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[3] / "templates"))


def _safe_json(s: Any) -> Any:
    if not s:
        return None
    if isinstance(s, dict | list):
        return s
    try:
        return json.loads(s)
    except (json.JSONDecodeError, TypeError):
        return s


def _human_status(status_obj: Any) -> tuple[str, int | None]:
    """Человекочитаемый статус и остаток для клиента."""
    remains: int | None = None
    if isinstance(status_obj, dict):
        for key in ("remains", "remain", "left", "remaining", "remaining_count"):
            if key in status_obj:
                try:
                    remains = int(float(str(status_obj[key])))
                    break
                except (TypeError, ValueError):
                    pass
        st = str(status_obj.get("status", "")).strip().lower()
        if supplier_canceled(status_obj):
            return "Заказ был отменён системой! Пожалуйста, свяжитесь с поддержкой.", (remains if remains is not None else 0)
        if st in {"completed", "done", "success", "finished"}:
            return "Завершён", 0
        if st in {"partial"}:
            return "Частично выполнен", remains
        if st in {"paused"}:
            return "Приостановлен. Если статус не меняется — напишите в поддержку.", remains
        if st in {"processing", "in progress", "progress", "working", "pending", "awaiting_balance"}:
            return "В работе", remains
        if st:
            return str(status_obj.get("status", "")).strip(), remains
    return "В работе", remains


def _is_final(status_obj: Any) -> bool:
    """Заказ завершён или отменён — перепроверять и обновлять страницу больше не нужно."""
    if supplier_canceled(status_obj):
        return True
    if isinstance(status_obj, dict):
        return str(status_obj.get("status", "")).strip().lower() in {"completed", "done", "success", "finished"}
    return False


@router.get("/status")
async def status_view(
    request: Request, code: str = "", uniquecode: str = "", session: AsyncSession = Depends(get_session)
) -> Response:
    code = (code or uniquecode).strip()
    if not code:
        return PlainTextResponse("Missing code", status_code=400)

    row = await repo.get_by_unique_code(session, code)
    if not row:
        return PlainTextResponse("Заказ не найден. Попробуйте позже или обратитесь в поддержку.", status_code=404)

    goods_id = str(row.get("goods_id") or "")
    ctx: dict[str, Any] = {
        "goods_name": config.services.goods_human.get(goods_id, goods_id),
        "days": int(row.get("days") or 0),
        "quantity": int(row.get("quantity") or 1),
        "link": row.get("tg_link") or "—",
        "footer": footer_for_platform(str(row.get("platform") or "")),
        "remains": "—",
        "auto_refresh": None,
        "top_note": None,
    }

    if str(row.get("status") or "") == "ERROR_INVALID_LINK":
        ctx |= {"status_line": INVALID_TG_LINK_MSG, "top_note": INVALID_TG_LINK_MSG}
        return _templates.TemplateResponse(request, "status.html", ctx)

    st_obj = _safe_json(row.get("supplier_status"))
    order_id = str(row.get("supplier_order_id") or "").strip()

    if order_id and not _is_final(st_obj):
        try:
            fresh = await fetch_status(str(row.get("supplier") or ""), order_id)
            await repo.update_supplier_by_ucode(session, code, json.dumps(fresh, ensure_ascii=False))
            st_obj = fresh
        except Exception as e:
            logger.warning(f"[STATUS] не удалось обновить статус code={code} order={order_id}: {type(e).__name__}: {e}")

    if not st_obj and order_id:
        ctx |= {
            "status_line": "Ожидаем запуск заказа...",
            "auto_refresh": 30,
            "top_note": "Проверяем заказ. Страница обновится автоматически.",
        }
        return _templates.TemplateResponse(request, "status.html", ctx)

    status_line, remains = _human_status(st_obj)
    # не финал → авто-обновление, чтобы клиент увидел «Завершён» без перезагрузки
    if not _is_final(st_obj):
        ctx["auto_refresh"] = 30
    ctx |= {"status_line": status_line, "remains": "—" if remains is None else str(remains)}
    return _templates.TemplateResponse(request, "status.html", ctx)
