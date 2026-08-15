import asyncio
import contextlib
from dataclasses import dataclass
import math
import random

import httpx
from loguru import logger
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.config.settings import Settings
from app.db import outbox_repository as outbox_repo
from app.db.outbox_repository import DueOutboxMessage
from app.services.outbox.delivery import DeliveryOutcome, OutboxDeliverer, OutboxDeliveryResult, classify_delivery_error


@dataclass(frozen=True, slots=True)
class OutboxWorkerConfig:
    poll_interval: float
    batch_size: int
    max_concurrency: int
    lease_seconds: int
    shutdown_grace: float
    backoff_base: float
    backoff_max: float
    cleanup_interval: float
    cleanup_retention_days: int

    def __post_init__(self) -> None:
        positive_values = {
            "poll_interval": self.poll_interval,
            "batch_size": self.batch_size,
            "max_concurrency": self.max_concurrency,
            "lease_seconds": self.lease_seconds,
            "shutdown_grace": self.shutdown_grace,
            "backoff_base": self.backoff_base,
            "backoff_max": self.backoff_max,
            "cleanup_interval": self.cleanup_interval,
            "cleanup_retention_days": self.cleanup_retention_days,
        }
        invalid = [name for name, value in positive_values.items() if value <= 0]
        if invalid:
            raise ValueError(f"Outbox settings must be positive: {', '.join(invalid)}")
        if self.backoff_base > self.backoff_max:
            raise ValueError("OUTBOX_BACKOFF_BASE must not exceed OUTBOX_BACKOFF_MAX")

    @classmethod
    def from_settings(cls, settings: Settings) -> "OutboxWorkerConfig":
        return cls(
            poll_interval=settings.OUTBOX_POLL_INTERVAL,
            batch_size=settings.OUTBOX_BATCH_SIZE,
            max_concurrency=settings.OUTBOX_MAX_CONCURRENCY,
            lease_seconds=settings.OUTBOX_LEASE_SECONDS,
            shutdown_grace=settings.OUTBOX_SHUTDOWN_GRACE_SECONDS,
            backoff_base=settings.OUTBOX_BACKOFF_BASE,
            backoff_max=settings.OUTBOX_BACKOFF_MAX,
            cleanup_interval=settings.OUTBOX_CLEANUP_INTERVAL,
            cleanup_retention_days=settings.OUTBOX_CLEANUP_RETENTION_DAYS,
        )


class OutboxWorker:
    def __init__(
        self,
        config: OutboxWorkerConfig,
        session_factory: async_sessionmaker[AsyncSession],
        deliverer: OutboxDeliverer,
    ) -> None:
        self._config = config
        self._session_factory = session_factory
        self._deliverer = deliverer
        self._delivery_task: asyncio.Task[None] | None = None
        self._cleanup_task: asyncio.Task[None] | None = None
        self._running = False
        self._stop_requested = asyncio.Event()

    def start(self) -> None:
        if self._delivery_task is not None and not self._delivery_task.done():
            return
        self._running = True
        self._stop_requested.clear()
        self._delivery_task = asyncio.create_task(self._delivery_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        logger.info("[OUTBOX] worker запущен")

    async def stop(self) -> None:
        self._running = False
        self._stop_requested.set()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._cleanup_task
        if self._delivery_task is not None:
            try:
                await asyncio.wait_for(asyncio.shield(self._delivery_task), timeout=self._config.shutdown_grace)
            except TimeoutError:
                logger.warning("[OUTBOX] timeout graceful shutdown, активная доставка будет отменена")
                self._delivery_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._delivery_task
        self._delivery_task = None
        self._cleanup_task = None
        logger.info("[OUTBOX] worker остановлен")

    async def _delivery_loop(self) -> None:
        while self._running:
            try:
                processed = await self.tick()
                if processed == 0:
                    await self._wait_for_next_poll()
            except asyncio.CancelledError:
                return
            except Exception as error:
                logger.opt(exception=error).error(f"[OUTBOX] ошибка цикла доставки: {error}")
                await self._wait_for_next_poll()

    async def _wait_for_next_poll(self) -> None:
        try:
            await asyncio.wait_for(self._stop_requested.wait(), timeout=self._config.poll_interval)
        except TimeoutError:
            pass

    async def _cleanup_loop(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(self._config.cleanup_interval)
                async with self._session_factory.begin() as session:
                    deleted = await outbox_repo.delete_delivered_before(session, self._config.cleanup_retention_days)
                if deleted:
                    logger.info(f"[OUTBOX] удалено старых доставленных сообщений: {deleted}")
            except asyncio.CancelledError:
                return
            except Exception as error:
                logger.opt(exception=error).error(f"[OUTBOX] ошибка очистки: {error}")

    async def tick(self) -> int:
        """Claim and deliver one batch. The claim transaction commits before any network call."""
        try:
            async with self._session_factory.begin() as session:
                events = await outbox_repo.claim_due(
                    session,
                    limit=min(self._config.batch_size, self._config.max_concurrency),
                    lease_seconds=self._config.lease_seconds,
                )
        except Exception as error:
            logger.opt(exception=error).error(f"[OUTBOX] не удалось арендовать сообщения: {error}")
            return 0

        results = await asyncio.gather(*(self._deliver_one(event) for event in events), return_exceptions=True)
        for event, result in zip(events, results, strict=True):
            if isinstance(result, BaseException):
                logger.opt(exception=result).error(
                    f"[OUTBOX] не удалось сохранить результат delivery_id={event.delivery_id}: {result}"
                )
        return len(events)

    async def _deliver_one(self, event: DueOutboxMessage) -> None:
        delivery_result: OutboxDeliveryResult | None = None
        try:
            delivery_result = await self._deliverer.deliver(event)
            delivery_result.response.raise_for_status()
            outcome = DeliveryOutcome.DELIVERED
            error_text = ""
        except Exception as error:
            outcome = classify_delivery_error(error)
            error_text = self._error_text(error)

        async with self._session_factory.begin() as session:
            if outcome == DeliveryOutcome.DELIVERED:
                updated = await self._deliverer.complete(session, event, delivery_result)
            elif outcome == DeliveryOutcome.DEAD:
                updated = await self._deliverer.fail(session, event, error_text)
            else:
                attempts = event.attempts + 1
                updated = await outbox_repo.reschedule(
                    session,
                    event.id,
                    event.lease_token,
                    error_text,
                    self.next_delay(attempts),
                )

        if not updated:
            logger.warning(f"[OUTBOX] устаревший результат проигнорирован delivery_id={event.delivery_id}")
            return
        if outcome == DeliveryOutcome.DELIVERED:
            try:
                await self._deliverer.after_complete(event, delivery_result)
            except Exception as error:
                logger.opt(exception=error).error(
                    f"[OUTBOX] post-delivery hook не выполнен delivery_id={event.delivery_id}: {error}"
                )
            logger.info(f"[OUTBOX] доставлено delivery_id={event.delivery_id}")
        elif outcome == DeliveryOutcome.DEAD:
            logger.error(f"[OUTBOX] dead delivery_id={event.delivery_id}: {error_text}")
        else:
            logger.warning(f"[OUTBOX] повтор delivery_id={event.delivery_id}: {error_text}")

    @staticmethod
    def _error_text(error: BaseException) -> str:
        text = f"{type(error).__name__}: {error}"
        if isinstance(error, httpx.HTTPStatusError):
            response_body = error.response.text.strip()
            if response_body:
                text = f"{text}; response={response_body[:1000]}"
        return text

    def next_delay(self, attempts: int) -> float:
        if self._config.backoff_base <= 0 or self._config.backoff_max <= 0:
            return 0
        max_doublings = max(0, math.ceil(math.log2(self._config.backoff_max / self._config.backoff_base)))
        doublings = min(max(0, attempts - 1), max_doublings)
        ceiling = min(self._config.backoff_base * (2**doublings), self._config.backoff_max)
        return random.uniform(0, ceiling)  # noqa: S311 - jitter does not require cryptographic randomness
