from fastapi import APIRouter, Depends
from fastapi.responses import PlainTextResponse, RedirectResponse, Response
from sqlalchemy.ext.asyncio import AsyncSession

from app.db.session import get_session
from app.services.orders._common import status_url
from app.services.orders.digiseller import process_digiseller

router = APIRouter()


@router.get("/digiseller-callback")
async def digiseller_callback(uniquecode: str = "", session: AsyncSession = Depends(get_session)) -> Response:
    code = uniquecode.strip()
    if not code:
        return PlainTextResponse("Missing 'uniquecode' query parameter.", status_code=400)
    await process_digiseller(session, code)
    return RedirectResponse(status_url(code), status_code=302)
