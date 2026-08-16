import json
from typing import Annotated

from pydantic import SecretStr, computed_field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


def _normalize_proxy_url(value: str) -> str | None:
    normalized = value.strip()
    if not normalized:
        return None
    if "://" not in normalized:
        return f"socks5://{normalized}"
    return normalized


def _normalize_proxy_urls(value: object) -> object:
    if not isinstance(value, str):
        return value
    parts = [part.strip() for line in value.splitlines() for part in line.split(",") if part.strip()]
    if not parts:
        return None
    normalized = [_normalize_proxy_url(part) for part in parts]
    return ",".join(item for item in normalized if item is not None)


class Settings(BaseSettings):
    PROJECT_NAME: str

    # Server Config
    SERVER_HOST: str
    SERVER_PORT: int
    USE_HTTPS: bool
    SSL_CHAIN_PATH: str | None
    SSL_KEY_PATH: str | None

    # Security
    SECRET_KEY: SecretStr
    ALGORITHM: str
    ACCESS_TOKEN_EXPIRE_MINUTES: int

    # CORS
    BACKEND_CORS_ORIGINS: Annotated[list[str], NoDecode]

    # Логирование настраивается в config/config.yaml (loguru), не через .env

    # Публичный домен: ссылки в TG + редиректы
    BASE_PUBLIC_URL: str

    # Telegram (уведомления в группу)
    TG_BOT_TOKEN: SecretStr
    TG_CHAT_ID: str
    TG_PROXY_ENABLED: bool
    TG_PROXY_URL: SecretStr | None

    # GGSEL
    GGSEL_SELLER_ID: int
    GGSEL_API_KEY: SecretStr

    # Digiseller / PLATI
    DIGI_SELLER_ID: str
    DIGI_API_KEY: SecretStr

    # TeaTeaGram: legacy, только статусы старых заказов
    TEA_API_KEY: SecretStr

    # Поставщик SMM Panel
    SMM_PANEL_BASE_URL: str
    SMM_PANEL_API_KEY: SecretStr

    # Поставщик Fragment: Telegram Stars и Premium
    FRAGMENT_BASE_URL: str
    FRAGMENT_API_KEY: SecretStr

    # Поведение
    AUTO_MARK_DELIVERED: bool
    INFLIGHT_TTL_SECONDS: int
    GGSEL_CHAT_POLL_INTERVAL: int
    STATUS_CHECK_DELAY_SECONDS: int
    # При первой встрече чата: True — проглотить историю, False — переслать
    GGSEL_CHAT_BOOTSTRAP_SILENT: bool
    # Страниц списка чатов за цикл
    GGSEL_CHAT_MAX_PAGES: int
    # Тянем чат, только если last_message не старше N дней
    GGSEL_CHAT_FRESH_DAYS: int

    # Поллер статусов заказов: интервал опроса, сек
    ORDER_POLL_INTERVAL: int
    # Заказов за цикл
    ORDER_POLL_BATCH: int
    # Бросаем опрос заказа, если он старше N часов
    ORDER_POLL_MAX_AGE_HOURS: int

    OUTBOX_POLL_INTERVAL: float = 1.0
    OUTBOX_BATCH_SIZE: int = 50
    OUTBOX_MAX_CONCURRENCY: int = 10
    OUTBOX_LEASE_SECONDS: int = 180
    OUTBOX_SHUTDOWN_GRACE_SECONDS: float = 30.0
    OUTBOX_BACKOFF_BASE: float = 2.0
    OUTBOX_BACKOFF_MAX: float = 300.0
    OUTBOX_CLEANUP_INTERVAL: float = 3600.0
    OUTBOX_CLEANUP_RETENTION_DAYS: int = 3

    # База данных (PostgreSQL через asyncpg)
    POSTGRES_HOST: str
    POSTGRES_USER: str
    POSTGRES_PASSWORD: SecretStr
    POSTGRES_DB: str
    POSTGRES_PORT: int
    POSTGRES_SCHEMA: str

    @computed_field  # type: ignore[prop-decorator]
    @property
    def DATABASE_URL(self) -> str:  # noqa: N802 — UPPER для единообразия с остальными настройками
        """SQLAlchemy URL (asyncpg) из отдельных POSTGRES_* настроек."""
        return (
            f"postgresql+asyncpg://{self.POSTGRES_USER}:{self.POSTGRES_PASSWORD.get_secret_value()}"
            f"@{self.POSTGRES_HOST}:{self.POSTGRES_PORT}/{self.POSTGRES_DB}"
        )

    @field_validator("TG_PROXY_URL", mode="before")
    @classmethod
    def normalize_proxy_url(cls, value: object) -> object:
        return _normalize_proxy_urls(value)

    @field_validator("BACKEND_CORS_ORIGINS", mode="before")
    @classmethod
    def parse_cors_origins(cls, value: object) -> list[str]:
        if isinstance(value, str):
            s = value.strip()
            if s.startswith("["):
                try:
                    parsed = json.loads(s)
                    if isinstance(parsed, list):
                        return [str(item).strip() for item in parsed if str(item).strip()]
                except json.JSONDecodeError:
                    pass
            return [item.strip() for item in s.split(",") if item.strip()]
        if isinstance(value, list):
            return value
        raise ValueError("BACKEND_CORS_ORIGINS must be a comma-separated string or JSON array")

    model_config = SettingsConfigDict(case_sensitive=True, env_file=".env", env_file_encoding="utf-8", extra="ignore")


settings = Settings()
