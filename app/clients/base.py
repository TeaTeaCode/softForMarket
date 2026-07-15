import asyncio
import time
from typing import Any, Literal

import httpx
from loguru import logger

# keep-alive выключен: висящие TLS-соединения на Windows дают ReadTimeout
DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=0)
DEFAULT_TIMEOUT = httpx.Timeout(connect=5.0, read=20.0, write=20.0, pool=5.0)

RETRY_STATUSES = frozenset({429, 500, 502, 503, 504, 522, 524})
RETRY_TOTAL = 3
RETRY_BACKOFF = 0.5

# сколько символов тела писать в лог
LOG_BODY_LIMIT = 1000

DEFAULT_HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; TG-Boost-Orders/1.0)",
    "Accept": "application/json",
}


def _trim(value: Any) -> str:
    if value is None:
        return "—"
    text = str(value)
    return text if len(text) <= LOG_BODY_LIMIT else f"{text[:LOG_BODY_LIMIT]}…(+{len(text) - LOG_BODY_LIMIT})"


class BaseApi:
    def __init__(
        self,
        base_url: str | None = None,
        timeout: float | httpx.Timeout | None = None,
        headers: dict[str, str] | None = None,
        proxy: str | None = None,
    ) -> None:
        merged_headers = {**DEFAULT_HEADERS, **(headers or {})}
        self._client = httpx.AsyncClient(
            base_url=base_url or "",
            timeout=timeout if timeout is not None else DEFAULT_TIMEOUT,
            headers=merged_headers,
            limits=DEFAULT_LIMITS,
            transport=httpx.AsyncHTTPTransport(retries=3),
            proxy=proxy,
            follow_redirects=True,
        )

    async def close(self) -> None:
        await self._client.aclose()

    async def request(
        self,
        method: Literal["GET", "POST", "PUT", "DELETE"],
        url: str,
        *,
        params: dict[str, Any] | None = None,
        json_data: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
        response_type: Literal["json", "text", "response"] = "json",
    ) -> Any:
        r = await self._perform(method, url, params=params, json_data=json_data, data=data, headers=headers)
        if response_type == "response":
            return r
        if response_type == "text":
            return r.text
        return r.json()

    async def _perform(
        self,
        method: str,
        url: str,
        params: dict[str, Any] | None = None,
        json_data: Any | None = None,
        data: Any | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        last_response: httpx.Response | None = None
        body = json_data if json_data is not None else data
        for attempt in range(RETRY_TOTAL + 1):
            try:
                logger.debug(f"[HTTP] → {method} {url} params={params} body={_trim(body)}")
                started = time.monotonic()
                r = await self._client.request(method, url, params=params, json=json_data, data=data, headers=headers)
                took = (time.monotonic() - started) * 1000
                logger.debug(f"[HTTP] ← {method} {url} {r.status_code} за {took:.0f}мс body={_trim(r.text)}")
                if r.status_code in RETRY_STATUSES and attempt < RETRY_TOTAL:
                    logger.warning(f"Ретрай {attempt + 1}/{RETRY_TOTAL}: {method} {url} -> {r.status_code} {r.text[:200]}")
                    last_response = r
                    await asyncio.sleep(RETRY_BACKOFF * (2**attempt))
                    continue
                r.raise_for_status()
                return r
            except httpx.HTTPStatusError as exc:
                logger.error(
                    f"Сервис вернул ошибку: {method} {exc.request.url} -> {exc.response.status_code} {exc.response.text[:200]}"
                )
                raise
            except (httpx.TransportError, httpx.TimeoutException) as exc:
                if attempt < RETRY_TOTAL:
                    logger.warning(f"Ретрай {attempt + 1}/{RETRY_TOTAL}: {method} {url} -> {type(exc).__name__}: {exc}")
                    await asyncio.sleep(RETRY_BACKOFF * (2**attempt))
                    continue
                cause = f" ({exc.__cause__!r})" if exc.__cause__ is not None else ""
                logger.error(f"Ошибка сетевого запроса: {method} {url} -> {type(exc).__name__}: {exc}{cause}")
                raise

        assert last_response is not None
        logger.error(
            f"Сервис вернул ошибку после {RETRY_TOTAL} ретраев: {method} {url} -> "
            f"{last_response.status_code} {last_response.text[:200]}"
        )
        last_response.raise_for_status()
        return last_response
