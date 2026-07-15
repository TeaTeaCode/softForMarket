from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.core.config.settings import settings

engine = create_async_engine(
    settings.DATABASE_URL,
    echo=False,
    future=True,
    pool_size=20,
    max_overflow=30,
    pool_timeout=30,
    pool_recycle=600,
    pool_pre_ping=True,
    connect_args={
        "server_settings": {"search_path": settings.POSTGRES_SCHEMA},
        "command_timeout": 60,
        "timeout": 60,
        "prepared_statement_cache_size": 0,
        "statement_cache_size": 0,
        "ssl": "disable",
    },
)

async_session = async_sessionmaker(engine, expire_on_commit=False)


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    async with async_session() as session:
        try:
            yield session
        finally:
            if session.in_transaction():
                await session.rollback()
            await session.close()
