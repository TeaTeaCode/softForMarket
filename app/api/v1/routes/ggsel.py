from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.orders._common import status_url
from app.services.orders.ggsel import process_ggsel

router = APIRouter()


@router.get("/ggsel")
async def ggsel_callback(uniquecode: str = "", unique_code: str = "", session: AsyncSession = Depends(get_session)) -> Response:
    code = (uniquecode or unique_code).strip()
    if not code:
        return PlainTextResponse("uniquecode is required", status_code=400)
    await process_ggsel(session, code)
    return RedirectResponse(status_url(code), status_code=302)
