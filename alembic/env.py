import asyncio
from logging.config import fileConfig

from sqlalchemy import pool, text
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import async_engine_from_config

from alembic import context

# this is the Alembic Config object, which provides
# access to the values within the .ini file in use.
config = context.config

# Interpret the config file for Python logging.
# This line sets up loggers basically.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

from app.core.config.settings import settings
from app.db.models import Base

# URL для sqlalchemy.url собирается из POSTGRES_* (см. alembic.ini, интерполяция %(DB_*)s).
section = config.config_ini_section
config.set_section_option(section, "DB_USER", settings.POSTGRES_USER)
config.set_section_option(section, "DB_PASSWORD", settings.POSTGRES_PASSWORD.get_secret_value())
config.set_section_option(section, "DB_HOST", settings.POSTGRES_HOST)
config.set_section_option(section, "DB_PORT", str(settings.POSTGRES_PORT))
config.set_section_option(section, "DB_NAME", settings.POSTGRES_DB)

target_metadata = Base.metadata
DB_SCHEMA = settings.POSTGRES_SCHEMA


def include_object(obj, name, type_, reflected, compare_to):  # type: ignore[no-untyped-def]
    # include_schemas=True заставляет alembic сравнивать все схемы; ограничиваемся своей
    # и не трогаем служебную таблицу версий.
    if type_ == "table":
        if name == "alembic_version":
            return False
        if obj.schema != DB_SCHEMA:
            return False
    return True


def run_migrations_offline() -> None:
    """Run migrations in 'offline' mode."""
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        version_table_schema=DB_SCHEMA,
        include_schemas=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        version_table_schema=DB_SCHEMA,
        include_schemas=True,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """In this scenario we need to create an Engine
    and associate a connection with the context.

    """
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    # Схему создаём отдельной зафиксированной транзакцией ДО миграций,
    # чтобы alembic_version и таблицы гарантированно легли в неё.
    async with connectable.begin() as connection:
        await connection.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{DB_SCHEMA}"'))

    async with connectable.connect() as connection:
        await connection.run_sync(do_run_migrations)

    await connectable.dispose()


def run_migrations_online() -> None:
    """Run migrations in 'online' mode."""
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
