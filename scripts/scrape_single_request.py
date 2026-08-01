"""One-shot scraper for a single enrichment_source_requests row.

Invoked by the Laravel `TriggerManualScrape` job (one call per source URL) to
scrape exactly one admin-submitted profile request by its DB id. Reuses the
existing ScrapingWorker logic (enricher routing, parsing, merge, status
transitions) — no duplicated fetch/merge code.

Usage:
    python scripts/scrape_single_request.py --request-id 12345

Exit codes:
    0  request processed (status is now completed / no_data / failed, set in DB)
    2  request not found
    3  invalid / unprocessable request (already complete, no URL, etc.)
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from pathlib import Path

import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Ensure project root is importable when run as a script.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from utils.logger import get_logger  # noqa: E402
from workers.scraping_worker import ScrapingWorker  # noqa: E402

logger = get_logger(__name__)
load_dotenv(override=True)


def _scraper_conn():
    """Open a connection to the scraper database (mirrors ScrapingWorker)."""
    return psycopg2.connect(
        host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
        database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
        user=os.getenv("SCRAPER_DB_USER", "scraper"),
        password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
    )


def load_request(request_id: int) -> dict | None:
    """Load a single request row, joined with the lawyer_enrichment context.

    The column set mirrors ScrapingWorker.fetch_pending_requests so the row can
    be handed straight to worker.process_request().
    """
    conn = _scraper_conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT
                    esr.id AS request_id,
                    esr.lawyer_enrichment_id,
                    esr.source_key,
                    esr.layer,
                    esr.source_profile_url,
                    esr.scrape_status,
                    esr.scrape_attempts,
                    esr.priority,
                    le.full_name,
                    le.first_name,
                    le.last_name,
                    le.bar_number,
                    le.license_state AS state,
                    le.city,
                    le.firm_name,
                    le.merged_data,
                    le.manually_curated_fields
                FROM enrichment_source_requests esr
                JOIN lawyer_enrichment le ON le.id = esr.lawyer_enrichment_id
                WHERE esr.id = %s
                """,
                (request_id,),
            )
            row = cursor.fetchone()
            return dict(row) if row else None
    finally:
        conn.close()


def is_processable(request: dict) -> tuple[bool, str]:
    """Guard: only scrape requests that are queued and have a URL."""
    if not request.get("source_profile_url"):
        return False, "no source_profile_url on request"
    status = request.get("scrape_status")
    # Allow queued; also allow 'pending' (a Save-draft later re-queued) and
    # 'failed'/'scraping' retries. Block only terminal-success states.
    if status == "completed":
        return False, "request already completed"
    return True, ""


async def run(request_id: int) -> int:
    request = load_request(request_id)
    if request is None:
        logger.error("Request not found", request_id=request_id)
        return 2

    ok, reason = is_processable(request)
    if not ok:
        logger.warning("Skipping request", request_id=request_id, reason=reason)
        return 3

    # Reuse the worker verbatim: it handles enricher routing, rate limiting,
    # status transitions (scraping -> completed/no_data/failed), and the merge
    # into lawyer_enrichment.merged_data.
    worker = ScrapingWorker(source_key=request["source_key"])
    success = await worker.process_request(request)

    logger.info(
        "Single scrape finished",
        request_id=request_id,
        source=request["source_key"],
        success=success,
        stats=worker.stats,
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Scrape a single enrichment_source_requests row by id.")
    parser.add_argument("--request-id", type=int, required=True, help="enrichment_source_requests.id")
    args = parser.parse_args()

    try:
        return asyncio.run(run(args.request_id))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
