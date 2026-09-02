"""Google Discovery batch worker (DB-as-queue).

Polls ``enrichment_batches`` for Filament-triggered "Start Google Discovery"
runs and executes the Google Text Search discovery pipeline (pull new popular
immigration lawyers) in the background, mirroring the Apify Avvo batch worker.
"""

from __future__ import annotations

import asyncio
import os
from datetime import datetime
from typing import Any, Dict, List, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

from workers.google_discovery_worker import GoogleDiscoveryWorker
from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv(override=True)


class GoogleDiscoveryBatchWorker:
    """Run Google Text Search discovery for batches created in the admin panel."""

    def __init__(self):
        self.worker_name = "google_discovery"

    def _conn(self):
        return psycopg2.connect(
            host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
            database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
            user=os.getenv("SCRAPER_DB_USER", "scraper"),
            password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
        )

    def _ensure_heartbeat_table(self):
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS worker_heartbeats (
                        worker_name TEXT PRIMARY KEY,
                        status TEXT,
                        detail JSONB,
                        last_heartbeat_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                    )
                    """
                )
            conn.commit()
        finally:
            conn.close()

    def _heartbeat(self, status: str, detail: Optional[Dict[str, Any]] = None):
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO worker_heartbeats (worker_name, status, detail, last_heartbeat_at)
                    VALUES (%s, %s, %s, NOW())
                    ON CONFLICT (worker_name) DO UPDATE SET
                        status = EXCLUDED.status,
                        detail = EXCLUDED.detail,
                        last_heartbeat_at = NOW()
                    """,
                    (self.worker_name, status, Json(detail or {})),
                )
            conn.commit()
        finally:
            conn.close()

    def _claim_batch(self) -> Optional[Dict[str, Any]]:
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_batches
                    SET metadata = metadata || jsonb_build_object('apify_run', jsonb_build_object('state', 'in_progress', 'claimed_at', %s)),
                        updated_at = NOW()
                    WHERE id = (
                        SELECT id FROM enrichment_batches
                        WHERE layer_name = 'google_discovery'
                          AND status = 'running'
                          AND metadata->'apify_run'->>'state' = 'requested'
                        ORDER BY created_at ASC
                        LIMIT 1
                        FOR UPDATE SKIP LOCKED
                    )
                    RETURNING id, batch_id, config_snapshot, metadata
                    """,
                    (datetime.now().isoformat(),),
                )
                row = cursor.fetchone()
                if row:
                    conn.commit()
                    return dict(row)
                return None
        finally:
            conn.close()

    def _update_batch(self, batch_id: str, **fields: Any):
        allowed = {
            "status", "total_records", "records_processed", "records_created",
            "records_updated", "records_skipped", "records_failed",
            "progress_percent", "current_record_index", "error_log",
            "error_count", "completed_at", "duration_seconds", "metadata",
        }
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return

        metadata = updates.pop("metadata", None)
        set_parts = [f"{k} = %s" for k in updates]
        params: List[Any] = list(updates.values())
        if metadata is not None:
            set_parts.append("metadata = COALESCE(metadata, '{}'::jsonb) || %s::jsonb")
            params.append(Json(metadata))
        if not set_parts:
            return

        set_clause = ", ".join(set_parts)
        params.append(batch_id)
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"UPDATE enrichment_batches SET {set_clause}, updated_at = NOW() WHERE batch_id = %s",
                    params,
                )
            conn.commit()
        finally:
            conn.close()

    async def process_batch(self, batch: Dict[str, Any]):
        batch_id = batch["batch_id"]
        config = batch.get("config_snapshot") or {}
        states = [s.lower() for s in (config.get("states") or [])]
        limit = config.get("limit")

        self._heartbeat("running", {"batch_id": batch_id, "phase": "started"})
        self._update_batch(batch_id, metadata={"apify_run": {"state": "in_progress"}})

        try:
            worker = GoogleDiscoveryWorker(target_states=states or None, limit=limit)
            stats = await worker.run()

            created = int(stats.get("persisted_profiles") or stats.get("selected_profiles") or 0)
            self._update_batch(
                batch_id,
                status="completed",
                total_records=created,
                records_processed=created,
                records_created=created,
                progress_percent=100,
                completed_at=datetime.now(),
                metadata={"apify_run": {"state": "done"}, "stats": stats},
            )
            self._heartbeat("idle", {"last_batch": batch_id, "result": "completed"})
        except Exception as exc:
            logger.error("Google discovery batch failed", batch_id=batch_id, error=str(exc))
            self._update_batch(
                batch_id,
                status="failed",
                error_log=str(exc),
                error_count=1,
                completed_at=datetime.now(),
                metadata={"apify_run": {"state": "failed"}},
            )
            self._heartbeat("error", {"last_batch": batch_id, "error": str(exc)})

    async def run_batch_loop(self, poll_interval: int = 20, once: bool = False):
        self._ensure_heartbeat_table()
        logger.info("Google discovery batch poller started", poll_interval=poll_interval, once=once)
        while True:
            try:
                batch = self._claim_batch()
            except Exception as exc:
                logger.error("Google discovery batch loop error", error=str(exc), exc_info=True)
                self._heartbeat("error", {"error": str(exc)})
                if once:
                    break
                await asyncio.sleep(poll_interval)
                continue

            if batch:
                logger.info("Claimed Google discovery batch", batch_id=batch["batch_id"])
                await self.process_batch(batch)
                if once:
                    continue
            else:
                self._heartbeat("idle")
                if once:
                    logger.info("No queued Google discovery batches; exiting (--once)")
                    break

            await asyncio.sleep(poll_interval)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Google Discovery batch worker")
    parser.add_argument("--poll-batches", action="store_true", help="Poll enrichment_batches for Filament-triggered runs")
    parser.add_argument("--once", action="store_true", help="Process queued batches once then exit")
    parser.add_argument("--poll-interval", type=int, default=20)
    args = parser.parse_args()

    worker = GoogleDiscoveryBatchWorker()
    await worker.run_batch_loop(poll_interval=args.poll_interval, once=args.once)


if __name__ == "__main__":
    asyncio.run(main())
