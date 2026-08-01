"""Tests for the single-request one-shot scraper entrypoint.

These tests avoid a real DB and real enrichers by patching:
- scripts.scrape_single_request._scraper_conn  (return a fake cursor)
- workers.scraping_worker.ScrapingWorker.process_request  (record delegation)

The point is to verify the script's own logic (load, processability guard,
delegation, exit codes), not the worker's internals (those belong to worker
tests).
"""

import asyncio
import types
from unittest.mock import MagicMock, patch

import pytest

import scripts.scrape_single_request as ssr


def _fake_conn(row: dict | None):
    """Build a fake psycopg2 connection that yields `row` for the lone SELECT."""
    cursor = MagicMock()
    cursor.fetchall.return_value = [row] if row else []
    cursor.fetchone.return_value = row
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cursor
    conn.cursor.return_value.__exit__.return_value = False
    return conn, cursor


def _request_row(**overrides) -> dict:
    base = {
        "request_id": 42,
        "lawyer_enrichment_id": 7,
        "source_key": "google_maps",
        "layer": 3,
        "source_profile_url": "ChIJ123",
        "scrape_status": "queued",
        "scrape_attempts": 0,
        "priority": 10,
        "full_name": "Jane Doe",
        "first_name": "Jane",
        "last_name": "Doe",
        "bar_number": None,
        "state": "NY",
        "city": "New York",
        "firm_name": None,
        "merged_data": {},
        "manually_curated_fields": [],
    }
    base.update(overrides)
    return base


def test_load_request_returns_row(monkeypatch):
    row = _request_row()
    conn, _ = _fake_conn(row)
    monkeypatch.setattr(ssr, "_scraper_conn", lambda: conn)
    result = ssr.load_request(42)
    assert result == row


def test_load_request_not_found(monkeypatch):
    conn, _ = _fake_conn(None)
    monkeypatch.setattr(ssr, "_scraper_conn", lambda: conn)
    assert ssr.load_request(999) is None


def test_is_processable_requires_url():
    ok, reason = ssr.is_processable(_request_row(source_profile_url=None))
    assert not ok
    assert "url" in reason


def test_is_processable_blocks_completed():
    ok, reason = ssr.is_processable(_request_row(scrape_status="completed"))
    assert not ok
    assert "completed" in reason


def test_is_processable_allows_queued():
    ok, _ = ssr.is_processable(_request_row(scrape_status="queued"))
    assert ok


async def test_run_delegates_to_worker(monkeypatch):
    row = _request_row()
    conn, _ = _fake_conn(row)
    monkeypatch.setattr(ssr, "_scraper_conn", lambda: conn)

    delegated = {}

    class FakeWorker:
        def __init__(self, source_key=None, **kwargs):
            delegated["source_key"] = source_key
            self.stats = {"completed": 0}

        async def process_request(self, request):
            delegated["request"] = request
            return True

    with patch.object(ssr, "ScrapingWorker", FakeWorker):
        code = await ssr.run(42)

    assert code == 0
    assert delegated["source_key"] == "google_maps"
    assert delegated["request"]["request_id"] == 42


async def test_run_returns_2_when_missing(monkeypatch):
    conn, _ = _fake_conn(None)
    monkeypatch.setattr(ssr, "_scraper_conn", lambda: conn)
    assert await ssr.run(404) == 2


async def test_run_returns_3_when_not_processable(monkeypatch):
    # Already completed -> not processable.
    conn, _ = _fake_conn(_request_row(scrape_status="completed"))
    monkeypatch.setattr(ssr, "_scraper_conn", lambda: conn)
    assert await ssr.run(42) == 3
