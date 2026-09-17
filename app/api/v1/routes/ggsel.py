import json

from fastapi import APIRouter, Depends, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.orders._common import status_url
from app.services.orders.ggsel import process_ggsel

router = APIRouter()


@router.get("/ggsel")
async def ggsel_callback(uniquecode: str = "", unique_code: str = "", session: AsyncSession = Depends(get_session)) -> Response:
    return await _handle(uniquecode, unique_code, session)


# вариант «1 день» оффера 83180
ONE_DAY_VARIANT_ID = 33095693
ONE_DAY_MAX_QTY = 10


@router.api_route("/api/v1/ggsel/precheck/boost", methods=["GET", "POST"])
@router.api_route("/ggsel/precheck/boost", methods=["GET", "POST"])
async def ggsel_precheck_boost(request: Request) -> Response:
    body = (await request.body()).decode("utf-8", errors="replace")
    logger.info(
        f"ggsel precheck boost: method={request.method} query={dict(request.query_params)} "
        f"headers={dict(request.headers)} body={body}"
    )
    if _is_rejected(body):
        logger.warning(f"ggsel precheck boost: ✖ отказ body={body}")
        return JSONResponse({"result": "reject"}, status_code=400)
    return JSONResponse({"result": "ok"}, status_code=200)


def _is_rejected(body: str) -> bool:
    """Отказать в покупке? Неразобранное тело не блокируем."""
    try:
        data = json.loads(body)
        quantity = float(data["product"]["cnt"])
        variants = {opt.get("value") for opt in data.get("options", []) if opt.get("type") == "radio"}
    except (ValueError, TypeError, KeyError, AttributeError):
        return False

    return ONE_DAY_VARIANT_ID in variants and quantity > ONE_DAY_MAX_QTY


@router.get("/stars")
async def stars_callback(uniquecode: str = "", unique_code: str = "", session: AsyncSession = Depends(get_session)) -> Response:
    return await _handle(uniquecode, unique_code, session)


@router.get("/premium")
async def premium_callback(
    uniquecode: str = "", unique_code: str = "", session: AsyncSession = Depends(get_session)
) -> Response:
    return await _handle(uniquecode, unique_code, session)


async def _handle(uniquecode: str, unique_code: str, session: AsyncSession) -> Response:
    code = (uniquecode or unique_code).strip()
    if not code:
        return PlainTextResponse("uniquecode is required", status_code=400)
    await process_ggsel(session, code)
    return RedirectResponse(status_url(code), status_code=302)
