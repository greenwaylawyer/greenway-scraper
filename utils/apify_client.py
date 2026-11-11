"""Minimal Apify API v2 client.

Uses only aiohttp against the public HTTP API so we do not need the Apify SDK
or a browser. Shared by the Apify enricher and the bulk worker.

Endpoint reference:
  - Run actor:      POST /v2/acts/{actorId}/runs
  - Run status:     GET  /v2/actor-runs/{runId}
  - Dataset items:  GET  /v2/actor-runs/{runId}/dataset/items
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Tuple

import aiohttp

logger = logging.getLogger(__name__)

APIFY_BASE = "https://api.apify.com/v2"

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}
TRANSIENT_STATUSES = {408, 429, 500, 502, 503, 504}
DEFAULT_RETRY_DELAYS: Tuple[float, ...] = (2.0, 4.0, 8.0, 16.0)

# Cheap generic page fetcher. Reads Avvo listing, search and profile pages
# without being blocked (the dedicated Avvo actor started timing out in Oct 2026).
WEB_FETCH_ACTOR = "apify~web-fetch"

_TOKEN_QS_RE = re.compile(r"([?&]token=)[^&\s'\"]+", re.IGNORECASE)


def _redact_url_tokens(text: str) -> str:
    return _TOKEN_QS_RE.sub(r"\1[REDACTED]", text)


class ApifyError(RuntimeError):
    """Raised when an Apify API call fails or a run does not succeed."""


class ApifyClient:
    """Async client for the Apify Actors v2 HTTP API."""

    def __init__(
        self,
        token: Optional[str] = None,
        *,
        retry_delays: Iterable[float] = DEFAULT_RETRY_DELAYS,
        request_timeout_seconds: float = 60.0,
    ):
        self.token = token or os.getenv("APIFY_TOKEN", "")
        if not self.token:
            raise ApifyError("APIFY_TOKEN is not set in the environment")
        self.retry_delays = tuple(retry_delays)
        self.request_timeout_seconds = float(request_timeout_seconds)

    def _headers(self) -> Dict[str, str]:
        return {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }

    async def _request_json(
        self,
        method: str,
        url: str,
        action: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json_body: Optional[Dict[str, Any]] = None,
        ok_statuses: Tuple[int, ...] = (200,),
    ) -> Any:
        """Execute an Apify HTTP request with retries on transient HTTP/network failures."""
        delays = list(self.retry_delays)
        max_attempts = 1 + len(delays)
        timeout = aiohttp.ClientTimeout(total=self.request_timeout_seconds)

        for attempt in range(max_attempts):
            try:
                async with aiohttp.ClientSession(timeout=timeout) as session:
                    async with session.request(
                        method,
                        url,
                        params=params,
                        json=json_body,
                        headers=self._headers(),
                    ) as resp:
                        raw_text = await resp.text()
                        status = resp.status
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                safe_err = _redact_url_tokens(str(exc))
                if attempt < len(delays):
                    delay = delays[attempt]
                    logger.warning(
                        "Apify %s transient network error (attempt %d/%d, retrying in %.1fs): %s",
                        action,
                        attempt + 1,
                        max_attempts,
                        delay,
                        safe_err,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ApifyError(f"Apify {action} network error: {safe_err}") from exc

            if status in TRANSIENT_STATUSES:
                snippet = _redact_url_tokens(raw_text[:200]).replace("\n", " ").strip()
                if attempt < len(delays):
                    delay = delays[attempt]
                    logger.warning(
                        "Apify %s returned HTTP %d (attempt %d/%d, retrying in %.1fs)",
                        action,
                        status,
                        attempt + 1,
                        max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ApifyError(f"Apify {action} failed ({status}): {snippet}")

            if status not in ok_statuses:
                snippet = _redact_url_tokens(raw_text[:500])
                raise ApifyError(f"Apify {action} failed ({status}): {snippet}")

            try:
                return json.loads(raw_text)
            except ValueError as exc:
                snippet = _redact_url_tokens(raw_text[:200]).replace("\n", " ").strip()
                if attempt < len(delays):
                    delay = delays[attempt]
                    logger.warning(
                        "Apify %s returned non-JSON HTTP %d (attempt %d/%d, retrying in %.1fs)",
                        action,
                        status,
                        attempt + 1,
                        max_attempts,
                        delay,
                    )
                    await asyncio.sleep(delay)
                    continue
                raise ApifyError(f"Apify {action} returned invalid JSON: {snippet}") from exc

        raise ApifyError(f"Apify {action} exhausted retries")

    async def run_actor(
        self,
        actor_id: str,
        input_data: Dict[str, Any],
        *,
        build: Optional[str] = None,
        memory_mbytes: Optional[int] = None,
        timeout_seconds: Optional[int] = None,
    ) -> str:
        """Start an actor run and return the run ID."""
        params: Dict[str, Any] = {}
        if build:
            params["build"] = build
        if memory_mbytes:
            params["memory"] = memory_mbytes
        if timeout_seconds:
            params["timeout"] = timeout_seconds

        url = f"{APIFY_BASE}/acts/{actor_id}/runs"
        payload = await self._request_json(
            "POST",
            url,
            "run_actor",
            params=params or None,
            json_body=input_data,
            ok_statuses=(200, 201),
        )
        run_id = payload.get("data", {}).get("id") if isinstance(payload, dict) else None
        if not run_id:
            raise ApifyError(f"Apify run_actor returned no run id: {payload}")
        return run_id

    async def get_run_status(self, run_id: str) -> str:
        """Return the run status string (READY, RUNNING, SUCCEEDED, ...)."""
        url = f"{APIFY_BASE}/actor-runs/{run_id}"
        payload = await self._request_json("GET", url, "get_run_status")
        return payload.get("data", {}).get("status")

    async def wait_for_finish(
        self,
        run_id: str,
        *,
        poll_seconds: int = 5,
        timeout_seconds: Optional[int] = None,
        on_status: Optional[Any] = None,
    ) -> str:
        """Poll until the run reaches a terminal status; return the status.

        If ``on_status`` is provided it is awaited with the current run status
        after every poll, so callers can report progress (e.g. write a
        heartbeat or update a batch row) while the actor runs.
        """
        started_at = time.monotonic()
        while True:
            status = await self.get_run_status(run_id)
            if on_status is not None:
                await on_status(status)
            if status in TERMINAL_STATUSES:
                return status
            if timeout_seconds and (time.monotonic() - started_at) >= timeout_seconds:
                raise ApifyError(
                    f"Apify run {run_id} timed out after {timeout_seconds}s (status={status})"
                )
            await asyncio.sleep(poll_seconds)

    async def get_dataset_items(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Fetch a page of dataset items for a finished run."""
        url = f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items"
        payload = await self._request_json(
            "GET",
            url,
            "get_dataset_items",
            params={"offset": offset, "limit": limit},
        )
        if not isinstance(payload, list):
            raise ApifyError(f"Apify dataset items unexpected shape: {type(payload)}")
        return payload

    async def fetch_page(
        self,
        url: str,
        formats: List[str],
        *,
        timeout_seconds: int = 180,
    ) -> Optional[Dict[str, Any]]:
        """Fetch one URL with apify/web-fetch and return its dataset item.

        Returns None when the run does not succeed or the page answered with an
        HTTP error. The timeout is passed to Apify too, so a stuck run is killed
        on Apify's side instead of running to the 1-hour default.
        """
        run_id = await self.run_actor(
            WEB_FETCH_ACTOR,
            {"url": url, "formats": formats},
            timeout_seconds=timeout_seconds,
        )
        status = await self.wait_for_finish(
            run_id, poll_seconds=3, timeout_seconds=timeout_seconds + 30
        )
        if status != "SUCCEEDED":
            return None
        items = await self.get_dataset_items(run_id, limit=1)
        item = items[0] if items else None
        if item is None:
            return None
        http_status = (item.get("fetch") or {}).get("httpStatusCode")
        if http_status is not None and int(http_status) >= 400:
            return None
        return item

    async def fetch_html(self, url: str, *, timeout_seconds: int = 180) -> Optional[str]:
        """Return the raw HTML of a page via apify/web-fetch, or None."""
        item = await self.fetch_page(url, ["raw"], timeout_seconds=timeout_seconds)
        html = (item or {}).get("raw")
        return html if isinstance(html, str) and html.strip() else None
