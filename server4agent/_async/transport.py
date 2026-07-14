from __future__ import annotations

import asyncio
import time
from typing import Any, Mapping, Optional

import httpx

from .. import _transport as t
from ..errors import APIConnectionError, APITimeoutError


class AsyncTransport:
    """Async HTTP with timeouts, method-aware retries, and typed errors."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        *,
        timeout: float = t.DEFAULT_TIMEOUT,
        max_retries: int = t.DEFAULT_MAX_RETRIES,
        client: Optional[httpx.AsyncClient] = None,
    ) -> None:
        self.max_retries = max_retries
        self._client = client or httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=timeout,
            headers=t.default_headers(api_key),
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[Mapping[str, Any]] = None,
        body: Any = None,
    ) -> Any:
        params = t.clean_params(query)
        attempt = 0
        while True:
            t.logger.debug("--> %s %s", method, path)
            started = time.perf_counter()
            try:
                resp = await self._client.request(method, path, params=params, json=body)
            except httpx.TimeoutException:
                if t.can_retry_exception(method, attempt, self.max_retries):
                    await asyncio.sleep(t.retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise APITimeoutError() from None
            except httpx.TransportError as exc:
                if t.can_retry_exception(method, attempt, self.max_retries):
                    await asyncio.sleep(t.retry_delay(attempt, None))
                    attempt += 1
                    continue
                raise APIConnectionError(str(exc)) from None

            elapsed_ms = (time.perf_counter() - started) * 1000
            t.logger.debug("<-- %s %s %s (%.0fms)", method, path, resp.status_code, elapsed_ms)

            if t.should_retry_status(method, resp.status_code, attempt, self.max_retries):
                delay = t.retry_delay(attempt, resp.headers.get("retry-after"))
                t.logger.debug("... retrying in %.2fs (attempt %d)", delay, attempt + 1)
                await asyncio.sleep(delay)
                attempt += 1
                continue

            data = t.decode_json(resp.content)
            if resp.status_code >= 400:
                t.raise_for_status(resp.status_code, data, resp.headers)
            return data

    async def aclose(self) -> None:
        await self._client.aclose()
