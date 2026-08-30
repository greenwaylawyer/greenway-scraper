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
import os
from typing import Any, Dict, List, Optional

import aiohttp

APIFY_BASE = "https://api.apify.com/v2"

TERMINAL_STATUSES = {"SUCCEEDED", "FAILED", "ABORTED", "TIMED-OUT"}


class ApifyError(RuntimeError):
    """Raised when an Apify API call fails or a run does not succeed."""


class ApifyClient:
    """Async client for the Apify Actors v2 HTTP API."""

    def __init__(self, token: Optional[str] = None):
        self.token = token or os.getenv("APIFY_TOKEN", "")
        if not self.token:
            raise ApifyError("APIFY_TOKEN is not set in the environment")

    def _headers(self) -> Dict[str, str]:
        return {"Content-Type": "application/json"}

    def _params(self, extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        params = {"token": self.token}
        if extra:
            params.update(extra)
        return params

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
        extra: Dict[str, Any] = {}
        if build:
            extra["build"] = build
        if memory_mbytes:
            extra["memory"] = memory_mbytes
        if timeout_seconds:
            extra["timeout"] = timeout_seconds

        url = f"{APIFY_BASE}/acts/{actor_id}/runs"
        async with aiohttp.ClientSession() as session:
            async with session.post(
                url, params=self._params(extra), json=input_data, headers=self._headers()
            ) as resp:
                payload = await resp.json()
                if resp.status not in (200, 201):
                    raise ApifyError(
                        f"Apify run_actor failed ({resp.status}): {payload}"
                    )
        run_id = payload.get("data", {}).get("id")
        if not run_id:
            raise ApifyError(f"Apify run_actor returned no run id: {payload}")
        return run_id

    async def get_run_status(self, run_id: str) -> str:
        """Return the run status string (READY, RUNNING, SUCCEEDED, ...)."""
        url = f"{APIFY_BASE}/actor-runs/{run_id}"
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=self._params()) as resp:
                payload = await resp.json()
                if resp.status != 200:
                    raise ApifyError(
                        f"Apify get_run_status failed ({resp.status}): {payload}"
                    )
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
        elapsed = 0.0
        while True:
            status = await self.get_run_status(run_id)
            if on_status is not None:
                await on_status(status)
            if status in TERMINAL_STATUSES:
                return status
            if timeout_seconds and elapsed >= timeout_seconds:
                raise ApifyError(
                    f"Apify run {run_id} timed out after {timeout_seconds}s (status={status})"
                )
            await asyncio.sleep(poll_seconds)
            elapsed += poll_seconds

    async def get_dataset_items(
        self,
        run_id: str,
        *,
        offset: int = 0,
        limit: int = 1000,
    ) -> List[Dict[str, Any]]:
        """Fetch a page of dataset items for a finished run."""
        url = f"{APIFY_BASE}/actor-runs/{run_id}/dataset/items"
        params = self._params({"offset": offset, "limit": limit})
        async with aiohttp.ClientSession() as session:
            async with session.get(url, params=params) as resp:
                if resp.status != 200:
                    text = await resp.text()
                    raise ApifyError(
                        f"Apify get_dataset_items failed ({resp.status}): {text}"
                    )
                payload = await resp.json()
        if not isinstance(payload, list):
            raise ApifyError(f"Apify dataset items unexpected shape: {type(payload)}")
        return payload
