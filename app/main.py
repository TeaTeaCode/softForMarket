from collections.abc import AsyncGenerator, Awaitable, Callable
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from app.api.v1.api import api_router
from app.clients.platforms.digiseller import digiseller
from app.clients.platforms.ggsel import ggsel
from app.clients.suppliers.fragment import fragment
from app.clients.suppliers.smm_panel import smm_panel
from app.clients.suppliers.teateagram import teateagram
from app.clients.telegram.client import telegram
from app.core.config.settings import settings
from app.core.logging import setup_logging


class ProxyHeadersMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
        if forwarded_for := request.headers.get("X-Forwarded-For"):
            client_ip = forwarded_for.split(",")[0].strip()
            request.scope["client"] = (client_ip, request.scope["client"][1])
        elif real_ip := request.headers.get("X-Real-IP"):
            request.scope["client"] = (real_ip, request.scope["client"][1])

        return await call_next(request)


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    setup_logging()
    static_dir = Path(__file__).resolve().parent.parent / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=static_dir), name="static")

    yield

    await ggsel.close()
    await digiseller.close()
    await teateagram.close()
    await smm_panel.close()
    await fragment.close()
    await telegram.close()


app = FastAPI(
    title=settings.PROJECT_NAME,
    openapi_url="/openapi.json",
    docs_url="/openapi",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[str(origin) for origin in settings.BACKEND_CORS_ORIGINS],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["Content-Disposition", "X-TelegramToolkit-Report"],
)

app.include_router(api_router)
