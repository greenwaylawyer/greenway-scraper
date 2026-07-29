"""AI merge worker for Google-first MVP."""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
import sys
from typing import Any, Dict, Optional

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from ai import config as ai_config
from ai.providers.factory import get_client
from pipeline.merge_engine import MergeEngine
from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv(override=True)


class AIMergeWorker:
    """Merge Google/Justia/Avvo data into publication-ready profile JSON."""

    def __init__(self, batch_size: int = 100, dry_run: bool = False):
        self.batch_size = batch_size
        self.dry_run = dry_run
        self.stats = {"checked": 0, "merged": 0, "fallback_merged": 0, "errors": 0}
        self._merge_engine = MergeEngine()
        self._client: Optional[Any] = None

    def _conn(self):
        return psycopg2.connect(
            host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
            database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
            user=os.getenv("SCRAPER_DB_USER", "scraper"),
            password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
        )

    def _client_or_none(self) -> Optional[Any]:
        if not ai_config.is_feature_enabled("merge_arbitration"):
            return None
        if self._client is None:
            try:
                self._client = get_client("merge_arbitration")
            except Exception as exc:
                # Gracefully degrade to deterministic merge if AI client is unavailable.
                logger.warning("AI merge client unavailable; using deterministic merge only", error=str(exc))
                return None
        return self._client

    def _extract_source_data(self, raw_data_by_source: Dict[str, Any], source_key: str) -> Dict[str, Any]:
        source_payload = raw_data_by_source.get(source_key) or {}
        if isinstance(source_payload, dict) and isinstance(source_payload.get("data"), dict):
            return source_payload["data"]
        return source_payload if isinstance(source_payload, dict) else {}

    def _deterministic_merge(self, merged_data: Dict[str, Any], raw_data_by_source: Dict[str, Any]) -> Dict[str, Any]:
        google = self._extract_source_data(raw_data_by_source, "google_maps") or self._extract_source_data(raw_data_by_source, "google_discovery")
        justia = self._extract_source_data(raw_data_by_source, "justia")
        avvo = self._extract_source_data(raw_data_by_source, "avvo")

        profile = dict(merged_data or {})
        source_aliases = {
            "google": ("google_discovery", "google_maps"),
            "justia": ("justia",),
            "avvo": ("avvo",),
        }
        existing_sources = set(profile.get("sources") or [])
        for canonical, aliases in source_aliases.items():
            if any(raw_data_by_source.get(alias) for alias in aliases):
                existing_sources.add(canonical)
        profile["sources"] = sorted(existing_sources)

        profile["full_name"] = profile.get("full_name") or justia.get("full_name") or avvo.get("full_name") or google.get("name")
        profile["email"] = profile.get("email") or justia.get("email") or avvo.get("email")
        profile["phone"] = profile.get("phone") or google.get("formatted_phone") or justia.get("phone") or avvo.get("phone")
        profile["website_url"] = profile.get("website_url") or google.get("website") or justia.get("website") or avvo.get("website")
        profile["google_rating"] = profile.get("google_rating") or google.get("google_rating") or google.get("rating")
        profile["google_review_count"] = profile.get("google_review_count") or google.get("google_review_count") or google.get("review_count")
        profile["avvo_rating"] = profile.get("avvo_rating") or avvo.get("avvo_rating") or avvo.get("rating")

        practice_areas = []
        for source in (profile, justia, avvo, google):
            values = source.get("practice_areas")
            if isinstance(values, list):
                practice_areas.extend(values)
        normalized_pas = []
        seen = set()
        for area in practice_areas:
            key = str(area).strip().lower()
            if key and key not in seen:
                seen.add(key)
                normalized_pas.append(str(area).strip())
        if normalized_pas:
            profile["practice_areas"] = normalized_pas

        profile["bio"] = profile.get("bio") or justia.get("bio") or avvo.get("bio")
        profile["photo_url"] = profile.get("photo_url") or justia.get("photo_url") or avvo.get("photo_url")
        profile["google_discovery_selected"] = profile.get("google_discovery_selected", True)
        return profile

    async def _ai_merge(self, profile_row: Dict[str, Any]) -> Dict[str, Any]:
        client = self._client_or_none()
        merged_data = profile_row.get("merged_data") or {}
        raw_data_by_source = profile_row.get("raw_data_by_source") or {}
        deterministic = self._deterministic_merge(merged_data, raw_data_by_source)
        if not client:
            self.stats["fallback_merged"] += 1
            return deterministic

        system = (
            "You merge lawyer profile data from Google, Justia, and Avvo. "
            "Return JSON only. Keep both google_rating and avvo_rating if present. "
            "Prefer Google for current address/geocode, Justia/Avvo for bio and email, "
            "union+dedupe practice areas, and preserve existing non-null fields."
        )
        user_payload = {
            "existing_merged_data": merged_data,
            "raw_data_by_source": raw_data_by_source,
            "required_fields": [
                "full_name",
                "email",
                "phone",
                "website_url",
                "bio",
                "practice_areas",
                "google_rating",
                "google_review_count",
                "avvo_rating",
            ],
        }
        response = await client.complete_json(
            system=system,
            user=json.dumps(user_payload),
            max_tokens=1200,
        )
        if not isinstance(response, dict):
            self.stats["fallback_merged"] += 1
            return deterministic
        deterministic.update(response)
        return deterministic

    def _select_batch(self):
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT id, merged_data, raw_data_by_source
                    FROM lawyer_enrichment
                    WHERE promoted_at IS NULL
                      AND COALESCE((merged_data->>'google_discovery_selected')::boolean, false) = true
                    ORDER BY updated_at DESC
                    LIMIT %s
                    """,
                    (self.batch_size,),
                )
                return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def _persist(self, profile_id: int, merged_profile: Dict[str, Any]):
        score = self._merge_engine.calculate_completeness_score(merged_profile)
        if self.dry_run:
            return
        conn = self._conn()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE lawyer_enrichment
                    SET merged_data = %s,
                        completeness_score = %s,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (Json(merged_profile), score, profile_id),
                )
            conn.commit()
        finally:
            conn.close()

    async def run_once(self) -> Dict[str, int]:
        rows = self._select_batch()
        for row in rows:
            self.stats["checked"] += 1
            try:
                merged = await self._ai_merge(row)
                self._persist(row["id"], merged)
                self.stats["merged"] += 1
            except Exception as exc:
                logger.warning("AI merge failed for profile", profile_id=row.get("id"), error=str(exc))
                self.stats["errors"] += 1
        return self.stats


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="AI merge worker")
    parser.add_argument("--batch-size", type=int, default=100)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    stats = await AIMergeWorker(batch_size=args.batch_size, dry_run=args.dry_run).run_once()
    logger.info("AI merge run complete", **stats)


if __name__ == "__main__":
    asyncio.run(main())
