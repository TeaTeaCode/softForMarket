from fastapi import APIRouter

from app.api.v1.routes import digiseller, ggsel, status

api_router = APIRouter()
api_router.include_router(ggsel.router)
api_router.include_router(digiseller.router)
api_router.include_router(status.router)


@api_router.get("/health")
async def health() -> bool:
    return True
