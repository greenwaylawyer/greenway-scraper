"""Apify Avvo bulk enrichment worker.

Two modes:

- ``enrich``  — pull target profiles (seeded by Google discovery) and run the
  Apify Avvo actor over their Avvo search URLs, then match the returned
  profiles back to the targets and merge. One actor run for the whole batch.
- ``discover`` — paged category sweep for brand-new ``lawyer_enrichment`` rows.
  The actor has no paging input (``searchByCategory`` + ``searchByLocation``
  always return the same top N), so we walk Avvo's own listing pages
  (``/immigration-lawyer/ca.html?page=N``) with the cheap ``apify/web-fetch``
  actor, drop lawyers we already have (by Avvo ID), and send only new profile
  URLs to the paid Avvo actor. A cursor per (category, state) in
  ``apify_avvo_sweep`` makes the next run continue where the last one stopped.

The worker can also poll the ``enrichment_batches`` table for rows created by
the Filament admin ("Start Apify Avvo Run") and execute them, reporting
progress back to the batch row so the admin can monitor it in the panel.
"""

from __future__ import annotations

import asyncio
import math
import hashlib
import os
import re
from datetime import datetime
from difflib import SequenceMatcher
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

from config.loader import get_source_config
from pipeline.merge_engine import merge_scraped_data
from scrapers.enrichers.apify_avvo import map_actor_item
from utils.apify_client import ApifyClient, ApifyError
from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv(override=True)


AVVO_BASE = "https://www.avvo.com"
LISTING_FETCH_ACTOR = "apify~web-fetch"
PROFILE_URL_RE = re.compile(r"^https?://www\.avvo\.com/attorneys/[^?#]*?-(\d+)\.html")
# Avvo IDs also appear as the trailing number of stored profile URLs.
AVVO_ID_FROM_URL_RE = re.compile(r"-(\d+)\.html")


def avvo_id_from_url(url: Optional[str]) -> Optional[str]:
    match = AVVO_ID_FROM_URL_RE.search(url or "")
    return match.group(1) if match else None


def parse_listing_links(links: List[str], listing_url: str) -> Tuple[Dict[str, str], Optional[int]]:
    """Return ({avvo_id: canonical profile URL}, last page number) from a listing page's links."""
    profiles: Dict[str, str] = {}
    for link in links or []:
        match = PROFILE_URL_RE.match(link or "")
        if match and match.group(1) not in profiles:
            profiles[match.group(1)] = match.group(0)

    base = listing_url.split("?", 1)[0]
    last_page: Optional[int] = None
    for link in links or []:
        if not (link or "").startswith(base + "?"):
            continue
        page = re.search(r"[?&]page=(\d+)", link)
        if page and "sort=" not in link:
            last_page = max(last_page or 0, int(page.group(1)))
    return profiles, last_page


def _normalize(value: Any) -> str:
    return re.sub(r"[^a-z0-9 ]", "", (value or "").lower()).strip()


def _last_token(name: str) -> str:
    parts = name.split()
    return parts[-1] if parts else ""


class ApifyBulkWorker:
    """Bulk-enrich or discover profiles with the Apify Avvo actor."""

    def __init__(
        self,
        mode: str = "enrich",
        batch_size: int = 1000,
        dry_run: bool = False,
        limit: Optional[int] = None,
        states: Optional[List[str]] = None,
    ):
        self.mode = mode
        self.batch_size = batch_size
        self.dry_run = dry_run
        self.limit = limit
        self.states = [s.lower() for s in (states or [])]
        self.source_key = "avvo"
        self.worker_name = "apify_avvo"
        self._batch_id: Optional[str] = None
        self._on_apify_status: Optional[Any] = None
        self.client = ApifyClient()
        self.stats = {
            "selected": 0,
            "queries": 0,
            "items": 0,
            "matched": 0,
            "unmatched": 0,
            "inserted": 0,
            "merged": 0,
            "errors": 0,
            # discover mode
            "listing_pages": 0,
            "listings_seen": 0,
            "already_known": 0,
            "new_profiles_queued": 0,
        }

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

    def _config(self) -> Dict[str, Any]:
        return get_source_config(self.source_key) or {}

    def _actor_id(self) -> str:
        apify = self._config().get("apify", {})
        return apify.get("actor_id") or os.getenv("APIFY_AVVO_ACTOR_ID", "")

    def _input(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        base = self._config().get("apify", {}).get("input") or {}
        input_data: Dict[str, Any] = {}
        if isinstance(base, dict):
            input_data.update(base)
        input_data.update(extra)
        return input_data

    async def _run_actor(
        self,
        input_data: Dict[str, Any],
        on_status: Optional[Any] = None,
    ) -> List[Dict[str, Any]]:
        actor_id = self._actor_id()
        if not actor_id:
            raise ApifyError("No Apify actor_id configured for apify_avvo")
        timeout = self._config().get("apify", {}).get("timeout_seconds", 1800)
        run_id = await self.client.run_actor(actor_id, input_data)
        logger.info("Apify Avvo run started", run_id=run_id, mode=self.mode)

        async def _report(status: str) -> None:
            callback = on_status or self._on_apify_status
            if callback is not None:
                await callback(status)

        status = await self.client.wait_for_finish(
            run_id, poll_seconds=5, timeout_seconds=timeout, on_status=_report
        )
        if status != "SUCCEEDED":
            raise ApifyError(f"Apify Avvo run {run_id} ended with status {status}")
        items = await self._collect_all_items(run_id)
        logger.info("Apify Avvo run finished", run_id=run_id, items=len(items))
        return items

    async def _collect_all_items(self, run_id: str) -> List[Dict[str, Any]]:
        items: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = await self.client.get_dataset_items(run_id, offset=offset, limit=1000)
            if not page:
                break
            items.extend(page)
            offset += len(page)
            if len(page) < 1000:
                break
        return items

    @staticmethod
    def _profile_items(items: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        return [it for it in items if it.get("type") in (None, "profile")]

    # ── enrich mode ──────────────────────────────────────────────────────────

    def select_targets(self) -> List[Dict[str, Any]]:
        conn = self._conn()
        query = """
            SELECT le.id, le.full_name, le.first_name, le.last_name,
                   le.city, le.state, le.license_state, le.bar_number,
                   le.merged_data, le.manually_curated_fields
            FROM lawyer_enrichment le
            WHERE COALESCE((le.merged_data->>'google_discovery_selected')::boolean, false) = true
              AND NOT EXISTS (
                  SELECT 1 FROM enrichment_source_requests esr
                  WHERE esr.lawyer_enrichment_id = le.id
                    AND esr.source_key = %s
                    AND esr.scrape_status = 'completed'
              )
            ORDER BY COALESCE((le.merged_data->>'google_popularity_score')::numeric, 0) DESC NULLS LAST,
                     le.updated_at DESC
            LIMIT %s
        """
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(query, (self.source_key, self.batch_size))
                return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def _build_search_url(self, target: Dict[str, Any]) -> str:
        full_name = target.get("full_name") or " ".join(
            filter(None, [target.get("first_name"), target.get("last_name")])
        )
        city = (target.get("city") or "").strip()
        state = (target.get("license_state") or target.get("state") or "").strip()
        location = ", ".join(filter(None, [city, state]))
        return "https://www.avvo.com/search/lawyer_search?" + urlencode(
            {"q": full_name, "loc": location}
        )

    def _target_signature(self, target: Dict[str, Any]) -> Dict[str, str]:
        full_name = target.get("full_name") or " ".join(
            filter(None, [target.get("first_name"), target.get("last_name")])
        )
        merged = target.get("merged_data") or {}
        address = merged.get("address") or {}
        city = target.get("city") or address.get("city") or ""
        state = target.get("license_state") or target.get("state") or address.get("state") or ""
        return {
            "name": _normalize(full_name),
            "last": _normalize(target.get("last_name")) or _last_token(_normalize(full_name)),
            "first": _normalize(target.get("first_name")),
            "city": _normalize(city),
            "state": _normalize(state),
        }

    def _item_signature(self, mapped: Dict[str, Any]) -> Dict[str, str]:
        address = mapped.get("address") or {}
        state = _normalize(mapped.get("license_state") or (address.get("state") if isinstance(address, dict) else ""))
        city = _normalize(address.get("city")) if isinstance(address, dict) else ""
        name = _normalize(mapped.get("full_name"))
        return {
            "name": name,
            "last": _last_token(name),
            "first": "",
            "city": city,
            "state": state,
        }

    @staticmethod
    def _score(t: Dict[str, str], i: Dict[str, str]) -> float:
        if t["name"] and i["name"] and t["name"] == i["name"]:
            score = 1.0
        elif t["last"] and i["last"] and t["last"] == i["last"]:
            score = 0.85
            if t["first"] and i["first"] and t["first"] == i["first"]:
                score += 0.10
        elif t["name"] and i["name"]:
            score = SequenceMatcher(None, t["name"], i["name"]).ratio() * 0.8
        else:
            score = 0.0

        if t["city"] and i["city"] and t["city"] == i["city"]:
            score += 0.05
        if t["state"] and i["state"] and t["state"] == i["state"]:
            score += 0.05
        return min(score, 1.0)

    def _match(self, targets: List[Dict[str, Any]], items: List[Dict[str, Any]]) -> List[Tuple[Dict[str, Any], Dict[str, Any]]]:
        sigs = [self._target_signature(t) for t in targets]
        by_state: Dict[str, List[int]] = {}
        for idx, sig in enumerate(sigs):
            by_state.setdefault(sig["state"] or "_", []).append(idx)

        used: set = set()
        matches: List[Tuple[Dict[str, Any], Dict[str, Any]]] = []

        for item in items:
            mapped = map_actor_item(item)
            if not mapped.get("full_name"):
                continue
            i_sig = self._item_signature(mapped)
            candidates = by_state.get(i_sig["state"] or "_", []) or list(range(len(targets)))

            best_idx, best_score = None, 0.0
            for idx in candidates:
                if idx in used:
                    continue
                score = self._score(sigs[idx], i_sig)
                if score > best_score:
                    best_score, best_idx = score, idx

            if best_idx is not None and best_score >= 0.85:
                used.add(best_idx)
                matches.append((targets[best_idx], mapped))

        self.stats["items"] = len(items)
        self.stats["matched"] = len(matches)
        self.stats["unmatched"] = len(items) - len(matches)
        return matches

    # ── persist ──────────────────────────────────────────────────────────────

    def _merge_result(self, current: Optional[Dict[str, Any]], mapped: Dict[str, Any], curated: List[str]) -> Tuple[Dict[str, Any], int]:
        result = merge_scraped_data(
            lawyer_enrichment={
                "merged_data": current or {},
                "manually_curated_fields": curated or [],
            },
            scraped_data=mapped,
            source_key=self.source_key,
            layer=3,
        )
        return result["merged_data"], result["completeness_score"]

    def _upsert_request(self, target_id: int, source_url: Optional[str], mapped: Dict[str, Any]):
        conn = self._conn()
        batch_id = getattr(self, "_batch_id", None)
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO enrichment_source_requests (
                        lawyer_enrichment_id, source_key, layer,
                        source_profile_url, discovery_status, scrape_status,
                        discovery_method, scraped_data, scraped_at,
                        merged_to_profile, merged_at, requested_by, priority,
                        batch_id, created_at, updated_at
                    ) VALUES (%s, %s, 3, %s, 'found', 'completed',
                              'apify_bulk', %s::jsonb, NOW(),
                              true, NOW(), 'apify_bulk_worker', 1,
                              %s, NOW(), NOW())
                    ON CONFLICT (lawyer_enrichment_id, source_key) DO UPDATE SET
                        source_profile_url = EXCLUDED.source_profile_url,
                        discovery_status = 'found',
                        scrape_status = 'completed',
                        discovery_method = 'apify_bulk',
                        scraped_data = EXCLUDED.scraped_data,
                        scraped_at = NOW(),
                        merged_to_profile = true,
                        merged_at = NOW(),
                        batch_id = COALESCE(EXCLUDED.batch_id, enrichment_source_requests.batch_id),
                        updated_at = NOW()
                    """,
                    (target_id, self.source_key, source_url, Json(mapped), batch_id),
                )
            conn.commit()
        finally:
            conn.close()

    def _update_enrichment(self, target_id: int, merged_data: Dict[str, Any], score: int, source_url: Optional[str], mapped: Dict[str, Any]):
        threshold = int(os.getenv("COMPLETENESS_THRESHOLD", 80))
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE lawyer_enrichment
                    SET merged_data = %s,
                        completeness_score = %s,
                        raw_data_by_source = raw_data_by_source || jsonb_build_object(
                            %s, jsonb_build_object('scraped_at', %s, 'layer', 3, 'source_url', %s, 'data', %s::jsonb)
                        ),
                        enrichment_layers = CASE
                            WHEN NOT (enrichment_layers @> jsonb_build_array(%s))
                            THEN enrichment_layers || jsonb_build_array(%s)
                            ELSE enrichment_layers
                        END,
                        enrichment_level = GREATEST(enrichment_level, 3),
                        level_3_completed_at = COALESCE(level_3_completed_at, NOW()),
                        last_enriched_at = NOW(),
                        ready_for_promotion = (%s >= %s AND NOT COALESCE(promotion_blocked, false) AND promoted_at IS NULL),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        Json(merged_data),
                        score,
                        self.source_key,
                        datetime.now().isoformat(),
                        source_url,
                        Json(mapped),
                        self.source_key,
                        self.source_key,
                        score,
                        threshold,
                        target_id,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

    def _persist_match(self, target: Dict[str, Any], mapped: Dict[str, Any]):
        merged_data, score = self._merge_result(
            target.get("merged_data"), mapped, target.get("manually_curated_fields") or []
        )
        self._update_enrichment(target["id"], merged_data, score, mapped.get("source_profile_url"), mapped)
        self._upsert_request(target["id"], mapped.get("source_profile_url"), mapped)

    def _fingerprint(self, mapped: Dict[str, Any]) -> str:
        avvo_id = mapped.get("avvo_id")
        if avvo_id:
            return hashlib.sha256(f"avvo:{avvo_id}".encode()).hexdigest()
        address = mapped.get("address") or {}
        key = "|".join([
            _normalize(mapped.get("full_name")),
            _normalize(address.get("city") if isinstance(address, dict) else ""),
            _normalize(mapped.get("license_state")),
        ])
        return hashlib.sha256(key.encode()).hexdigest()

    def _insert_discovered(self, mapped: Dict[str, Any]):
        merged_data, score = self._merge_result({}, mapped, [])
        address = mapped.get("address") or {}
        city = address.get("city") if isinstance(address, dict) else None
        state = mapped.get("license_state") or (address.get("state") if isinstance(address, dict) else None)
        full_name = mapped.get("full_name")
        fingerprint = self._fingerprint(mapped)
        source_url = mapped.get("source_profile_url")
        threshold = int(os.getenv("COMPLETENESS_THRESHOLD", 80))
        ready_for_promotion = (score >= threshold)

        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    INSERT INTO lawyer_enrichment (
                        fingerprint, enrichment_layers, enrichment_level,
                        raw_data_by_source, merged_data, completeness_score,
                        full_name, city, state, license_state,
                        level_3_completed_at, last_enriched_at, ready_for_promotion
                    ) VALUES (%s, %s::jsonb, 3, %s::jsonb, %s::jsonb, %s, %s, %s, %s, %s, NOW(), NOW(), %s)
                    ON CONFLICT (fingerprint) DO UPDATE SET
                        raw_data_by_source = lawyer_enrichment.raw_data_by_source || EXCLUDED.raw_data_by_source,
                        merged_data = lawyer_enrichment.merged_data || EXCLUDED.merged_data,
                        completeness_score = GREATEST(lawyer_enrichment.completeness_score, EXCLUDED.completeness_score),
                        full_name = COALESCE(lawyer_enrichment.full_name, EXCLUDED.full_name),
                        city = COALESCE(lawyer_enrichment.city, EXCLUDED.city),
                        state = COALESCE(lawyer_enrichment.state, EXCLUDED.state),
                        license_state = COALESCE(lawyer_enrichment.license_state, EXCLUDED.license_state),
                        level_3_completed_at = COALESCE(lawyer_enrichment.level_3_completed_at, NOW()),
                        last_enriched_at = NOW(),
                        ready_for_promotion = (GREATEST(lawyer_enrichment.completeness_score, EXCLUDED.completeness_score) >= %s AND NOT COALESCE(lawyer_enrichment.promotion_blocked, false) AND lawyer_enrichment.promoted_at IS NULL),
                        updated_at = NOW()
                    RETURNING id
                    """,
                    (
                        fingerprint,
                        Json([self.source_key]),
                        Json({self.source_key: {"scraped_at": datetime.now().isoformat(), "layer": 3, "data": mapped}}),
                        Json(merged_data),
                        score,
                        full_name,
                        city,
                        state,
                        state,
                        ready_for_promotion,
                        threshold,
                    ),
                )
                row = cursor.fetchone()
                lawyer_id = row[0]
            conn.commit()
        finally:
            conn.close()

        # Flag the profile as scraped from Avvo so the admin UI source icons
        # and enrichment_layers reflect it (discover mode previously only wrote
        # raw_data_by_source, not a request row).
        self._upsert_request(lawyer_id, source_url, mapped)

    # ── orchestration ────────────────────────────────────────────────────────

    async def _run_enrich(self) -> Dict[str, Any]:
        targets = self.select_targets()
        if self.limit is not None and self.limit >= 0:
            targets = targets[: self.limit]
        self.stats["selected"] = len(targets)
        if not targets:
            logger.info("No targets selected for Apify Avvo enrich")
            return self.stats

        urls = [self._build_search_url(t) for t in targets]
        self.stats["queries"] = len(urls)
        items = await self._run_actor(self._input({"startUrls": urls, "limit": 10}))
        profiles = self._profile_items(items)
        matches = self._match(targets, profiles)

        if self.dry_run:
            logger.info("Dry run — matched profiles", matched=len(matches))
            return self.stats

        for target, mapped in matches:
            try:
                self._persist_match(target, mapped)
                self.stats["merged"] += 1
            except Exception as exc:
                logger.warning("Failed to persist Apify Avvo profile", profile_id=target.get("id"), error=str(exc))
                self.stats["errors"] += 1
        return self.stats

    # ── discover mode (paged sweep) ─────────────────────────────────────────

    def _ensure_sweep_table(self):
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS apify_avvo_sweep (
                        category TEXT NOT NULL,
                        state TEXT NOT NULL,
                        next_page INTEGER NOT NULL DEFAULT 1,
                        last_page INTEGER,
                        pages_scanned INTEGER NOT NULL DEFAULT 0,
                        listings_seen INTEGER NOT NULL DEFAULT 0,
                        new_profiles INTEGER NOT NULL DEFAULT 0,
                        completed_at TIMESTAMPTZ,
                        updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        PRIMARY KEY (category, state)
                    )
                    """
                )
            conn.commit()
        finally:
            conn.close()

    def _load_slots(self, category: str, states: List[str]) -> List[Dict[str, Any]]:
        """Sweep cursors for the states, creating missing ones; finished sweeps restart after resweep_days."""
        resweep_days = int(self._discover_config().get("resweep_days", 30))
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                for state in states:
                    cursor.execute(
                        "INSERT INTO apify_avvo_sweep (category, state) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (category, state),
                    )
                cursor.execute(
                    """
                    UPDATE apify_avvo_sweep
                    SET next_page = 1, completed_at = NULL, updated_at = NOW()
                    WHERE category = %s AND state = ANY(%s)
                      AND completed_at IS NOT NULL
                      AND completed_at < NOW() - make_interval(days => %s)
                    """,
                    (category, states, resweep_days),
                )
                cursor.execute(
                    """
                    SELECT * FROM apify_avvo_sweep
                    WHERE category = %s AND state = ANY(%s) AND completed_at IS NULL
                    ORDER BY array_position(%s, state)
                    """,
                    (category, states, states),
                )
                rows = [dict(r) for r in cursor.fetchall()]
            conn.commit()
            return rows
        finally:
            conn.close()

    def _save_slot(self, slot: Dict[str, Any], pages: int, seen: int, new: int):
        if self.dry_run:
            return
        done = slot.get("last_page") is not None and slot["next_page"] > slot["last_page"]
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE apify_avvo_sweep
                    SET next_page = %s, last_page = %s,
                        pages_scanned = pages_scanned + %s,
                        listings_seen = listings_seen + %s,
                        new_profiles = new_profiles + %s,
                        completed_at = CASE WHEN %s THEN NOW() ELSE NULL END,
                        updated_at = NOW()
                    WHERE category = %s AND state = %s
                    """,
                    (slot["next_page"], slot.get("last_page"), pages, seen, new, done, slot["category"], slot["state"]),
                )
            conn.commit()
        finally:
            conn.close()

    def _known_avvo_ids(self) -> set:
        """Avvo IDs already in lawyer_enrichment (from stored avvo_id or profile URLs)."""
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    SELECT raw_data_by_source->'avvo'->'data'->>'avvo_id',
                           raw_data_by_source->'avvo'->'data'->>'source_profile_url',
                           merged_data->>'avvo_id',
                           merged_data->>'source_profile_url'
                    FROM lawyer_enrichment
                    WHERE raw_data_by_source ? 'avvo' OR merged_data ? 'avvo_id'
                       OR merged_data->>'source_profile_url' LIKE '%%avvo.com/attorneys/%%'
                    UNION ALL
                    SELECT NULL, source_profile_url, NULL, NULL
                    FROM enrichment_source_requests
                    WHERE source_key = 'avvo' AND source_profile_url IS NOT NULL
                    """
                )
                known = set()
                for raw_id, raw_url, merged_id, merged_url in cursor.fetchall():
                    # The actor's id is the profile slug ("90064-ca-name-75849.html").
                    for value in (raw_id, merged_id):
                        avvo_id = str(value) if value and str(value).isdigit() else avvo_id_from_url(value)
                        if avvo_id:
                            known.add(avvo_id)
                    for url in (raw_url, merged_url):
                        avvo_id = avvo_id_from_url(url) if "avvo.com" in (url or "") else None
                        if avvo_id:
                            known.add(avvo_id)
                return known
        finally:
            conn.close()

    def _discover_config(self) -> Dict[str, Any]:
        return self._config().get("apify", {}).get("discover") or {}

    async def _fetch_listing(self, url: str) -> Optional[Dict[str, Any]]:
        """Fetch one Avvo listing page via apify/web-fetch; None when it fails."""
        try:
            run_id = await self.client.run_actor(LISTING_FETCH_ACTOR, {"url": url, "formats": ["links"]})
            status = await self.client.wait_for_finish(run_id, poll_seconds=3, timeout_seconds=180)
            if status != "SUCCEEDED":
                logger.warning("Listing fetch failed", url=url, status=status)
                return None
            items = await self.client.get_dataset_items(run_id, limit=1)
        except ApifyError as exc:
            logger.warning("Listing fetch failed", url=url, error=str(exc))
            return None
        return items[0] if items else None

    async def _collect_new_profile_urls(self, category: str, states: List[str], budget: int) -> List[str]:
        """Walk listing pages from each state's cursor until `budget` new profile URLs are found.

        Every new profile on a scanned page is returned (the cursor moves past the page),
        so the result can exceed `budget` by up to one page.
        """
        max_pages = int(self._discover_config().get("max_listing_pages_per_run", 60))
        concurrency = int(self._discover_config().get("listing_concurrency", 5))

        known = self._known_avvo_ids()
        found: Dict[str, str] = {}

        for slot in self._load_slots(category, states):
            pages = seen = new = 0
            while len(found) < budget and self.stats["listing_pages"] < max_pages:
                last_page = slot.get("last_page")
                if last_page is not None and slot["next_page"] > last_page:
                    break
                # A page lists ~20 lawyers; fetch only as many pages as the remaining budget needs.
                needed_pages = math.ceil((budget - len(found)) / 15)
                window = min(concurrency, needed_pages, max_pages - self.stats["listing_pages"])
                page_numbers = [slot["next_page"] + i for i in range(window)]
                if last_page is not None:
                    page_numbers = [p for p in page_numbers if p <= last_page]

                base_url = f"{AVVO_BASE}/{category}/{slot['state']}.html"
                urls = [base_url if p == 1 else f"{base_url}?page={p}" for p in page_numbers]
                results = await asyncio.gather(*(self._fetch_listing(u) for u in urls))

                empty_page = False
                for page_no, url, item in zip(page_numbers, urls, results):
                    if item is None:
                        # Transient failure: stop here and retry this page next run.
                        empty_page = True
                        break
                    profiles, page_last = parse_listing_links(item.get("links") or [], base_url)
                    if page_last:
                        slot["last_page"] = max(slot.get("last_page") or 0, page_last)
                    slot["next_page"] = page_no + 1
                    pages += 1
                    self.stats["listing_pages"] += 1
                    if not profiles:
                        # Past the last page: mark the sweep done.
                        slot["last_page"] = page_no - 1 if page_no > 1 else 1
                        empty_page = True
                        break
                    seen += len(profiles)
                    for avvo_id, profile_url in profiles.items():
                        if avvo_id in known or avvo_id in found:
                            self.stats["already_known"] += 1
                            continue
                        found[avvo_id] = profile_url
                        new += 1
                await self._report_discover_progress(slot, len(found), budget)
                if empty_page:
                    break

            self.stats["listings_seen"] += seen
            self._save_slot(slot, pages, seen, new)
            logger.info(
                "Avvo sweep slot progress",
                category=category, state=slot["state"], next_page=slot["next_page"],
                last_page=slot.get("last_page"), pages=pages, seen=seen, new=new,
            )
            if len(found) >= budget or self.stats["listing_pages"] >= max_pages:
                break

        return list(found.values())

    async def _report_discover_progress(self, slot: Dict[str, Any], found: int, budget: int):
        if self._on_apify_status is None:
            return
        await self._on_apify_status(
            f"SCANNING {slot['state'].upper()} page {slot['next_page'] - 1}"
            f"{'/' + str(slot['last_page']) if slot.get('last_page') else ''}: {found}/{budget} new"
        )

    async def _run_discover(self) -> Dict[str, Any]:
        states = self.states or ["ca", "ny", "tx", "fl", "il", "nj", "ma", "ga", "wa", "nc"]
        category = self._discover_config().get("category", "immigration-lawyer")
        budget = self.limit if (self.limit is not None and self.limit > 0) else 100

        self._ensure_sweep_table()
        profile_urls = await self._collect_new_profile_urls(category, states, budget)
        self.stats["new_profiles_queued"] = len(profile_urls)
        self.stats["queries"] = len(profile_urls)

        if not profile_urls:
            logger.info("Avvo sweep found no new profiles", states=states, **self.stats)
            return self.stats

        if self.dry_run:
            logger.info("Dry run — new profile URLs", count=len(profile_urls), sample=profile_urls[:5])
            return self.stats

        # The actor's limit is per query and must be >= 10; a profile URL yields one item.
        items = await self._run_actor(self._input({"startUrls": profile_urls, "limit": 10}))
        profile_items = self._profile_items(items)
        self.stats["items"] = len(profile_items)

        for item in profile_items:
            try:
                self._insert_discovered(map_actor_item(item))
                self.stats["inserted"] += 1
            except Exception as exc:
                logger.warning("Failed to insert discovered profile", error=str(exc))
                self.stats["errors"] += 1
        return self.stats

    async def run(self) -> Dict[str, Any]:
        if self.mode == "discover":
            return await self._run_discover()
        return await self._run_enrich()

    # ── batch (Filament-triggered) orchestration ─────────────────────────────

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
                        WHERE layer_name = 'avvo'
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
        self._batch_id = batch_id
        config = batch.get("config_snapshot") or {}
        self.mode = config.get("mode", "enrich")
        self.limit = config.get("limit")
        self.states = [s.lower() for s in (config.get("states") or [])]

        async def on_apify_status(status: str) -> None:
            self._heartbeat("running", {"batch_id": batch_id, "apify_status": status})
            self._update_batch(
                batch_id,
                metadata={"apify_run": {"state": "in_progress", "apify_status": status}},
            )

        self._on_apify_status = on_apify_status

        self._update_batch(
            batch_id,
            total_records=self.limit or 0,
            metadata={"apify_run": {"state": "in_progress"}},
        )
        self._heartbeat("running", {"batch_id": batch_id, "phase": "started"})
        try:
            await self.run()
            self._update_batch(
                batch_id,
                status="completed",
                records_processed=self.stats["merged"] + self.stats["inserted"],
                records_created=self.stats["inserted"],
                records_updated=self.stats["merged"],
                records_skipped=self.stats["unmatched"],
                records_failed=self.stats["errors"],
                progress_percent=100,
                completed_at=datetime.now(),
                metadata={"apify_run": {"state": "done", "apify_status": "SUCCEEDED"}, "stats": self.stats},
            )
            self._heartbeat("idle", {"last_batch": batch_id, "result": "completed"})
        except Exception as exc:
            logger.error("Apify Avvo batch failed", batch_id=batch_id, error=str(exc))
            self._update_batch(
                batch_id,
                status="failed",
                error_log=str(exc),
                error_count=1,
                completed_at=datetime.now(),
                metadata={"apify_run": {"state": "failed"}, "stats": self.stats},
            )
            self._heartbeat("error", {"last_batch": batch_id, "error": str(exc)})
        finally:
            self._on_apify_status = None
            self._batch_id = None

    async def run_batch_loop(self, poll_interval: int = 20, once: bool = False):
        self._ensure_heartbeat_table()
        logger.info("Apify Avvo batch poller started", poll_interval=poll_interval, once=once)
        while True:
            try:
                batch = self._claim_batch()
            except Exception as exc:
                logger.error("Apify Avvo batch loop error", error=str(exc), exc_info=True)
                self._heartbeat("error", {"error": str(exc)})
                if once:
                    break
                await asyncio.sleep(poll_interval)
                continue

            if batch:
                logger.info("Claimed Apify Avvo batch", batch_id=batch["batch_id"])
                await self.process_batch(batch)
                if once:
                    continue
            else:
                self._heartbeat("idle")
                if once:
                    logger.info("No queued Apify Avvo batches; exiting (--once)")
                    break

            await asyncio.sleep(poll_interval)


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Apify Avvo bulk enrichment worker")
    parser.add_argument("--mode", choices=["enrich", "discover"], default="enrich")
    parser.add_argument("--batch-size", type=int, default=1000)
    parser.add_argument("--limit", type=int, default=None, help="Cap on profiles (enrich) or per-state listings (discover)")
    parser.add_argument("--states", nargs="*", help="State codes for discover mode")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--poll-batches", action="store_true", help="Poll enrichment_batches for Filament-triggered runs")
    parser.add_argument("--once", action="store_true", help="Process queued batches once then exit (on-demand)")
    parser.add_argument("--poll-interval", type=int, default=20)
    args = parser.parse_args()

    worker = ApifyBulkWorker(
        mode=args.mode, batch_size=args.batch_size, dry_run=args.dry_run,
        limit=args.limit, states=args.states,
    )

    if args.poll_batches:
        await worker.run_batch_loop(poll_interval=args.poll_interval, once=args.once)
        return

    stats = await worker.run()
    logger.info("Apify Avvo bulk complete", **stats)


if __name__ == "__main__":
    asyncio.run(main())
