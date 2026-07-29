"""Google-first discovery worker for MVP pipeline."""

from __future__ import annotations

import asyncio
from datetime import datetime
import os
from pathlib import Path
import sys
from typing import Any, Dict, List, Optional

import aiohttp
import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import get_mvp_google_first_config
from pipeline.google_discovery_filter import dedupe_and_rank, filter_individuals
from utils.cost_protection import CostProtectionSystem, CostProtectionError
from utils.logger import get_logger
from utils.rate_limiter import TokenBucketRateLimiter

logger = get_logger(__name__)
load_dotenv(override=True)


class GoogleDiscoveryWorker:
    """Discover popular individual lawyers using Google Text Search."""

    def __init__(
        self,
        target_states: Optional[List[str]] = None,
        batch_size: int = 200,
        dry_run: bool = False,
        limit: Optional[int] = None,
    ):
        self.config = get_mvp_google_first_config()
        if not self.config:
            raise ValueError("Missing mvp_google_first config in enrichment_sources.yaml")

        self.target_states = [s.upper() for s in (target_states or list(self.config.get("states", {}).keys()))]
        self.batch_size = batch_size
        self.dry_run = dry_run
        # Optional hard cap on the number of profiles persisted (after ranking).
        # The highest-popularity profiles are kept; lower-ranked ones are dropped.
        self.limit = limit
        self.api_key = os.getenv("GOOGLE_PLACES_API_KEY")
        self.rate_limiter = TokenBucketRateLimiter(rate=40)
        self.cost_protection = CostProtectionSystem()
        self.stats = {
            "states_processed": 0,
            "queries_executed": 0,
            "raw_candidates": 0,
            "individual_candidates": 0,
            "selected_profiles": 0,
            "persisted_profiles": 0,
        }

    def _scraper_conn(self):
        return psycopg2.connect(
            host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
            database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
            user=os.getenv("SCRAPER_DB_USER", "scraper"),
            password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
        )

    def _portal_conn(self):
        return psycopg2.connect(
            host=os.getenv("PORTAL_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("PORTAL_DB_PORT", 5433)),
            database=os.getenv("PORTAL_DB_NAME", "greenway"),
            user=os.getenv("PORTAL_DB_USER", "greenway"),
            password=os.getenv("PORTAL_DB_PASSWORD", "secret"),
        )

    def _fetch_cities(self, state_code: str) -> List[Dict[str, Any]]:
        conn = self._portal_conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    """
                    SELECT c.name, c.population, s.code AS state_code
                    FROM cities c
                    JOIN states s ON s.id = c.state_id
                    WHERE s.code = %s
                      AND c.population IS NOT NULL
                      AND c.population >= %s
                    ORDER BY c.population DESC, c.name ASC
                    """,
                    (state_code, self.config["city_tiers"]["tier4"]["min_population"]),
                )
                return [dict(r) for r in cursor.fetchall()]
        finally:
            conn.close()

    def _tier_for_population(self, population: int) -> str:
        tiers = self.config["city_tiers"]
        if population >= tiers["tier1"]["min_population"]:
            return "tier1"
        if population >= tiers["tier2"]["min_population"]:
            return "tier2"
        if population >= tiers["tier3"]["min_population"]:
            return "tier3"
        return "tier4"

    def _build_queries_for_city(self, city: str, state_code: str, population: int) -> List[str]:
        cfg = self.config
        tier = self._tier_for_population(population)
        tier_cfg = cfg["city_tiers"][tier]
        target_query_count = tier_cfg["query_count_min"]
        templates = cfg["google_api"]["query_templates"]
        practice_areas = cfg["practice_areas"]
        modifiers = ["", "best", "top rated", "experienced"]

        queries: List[str] = []
        for practice_area in practice_areas:
            for template in templates:
                for modifier in modifiers:
                    prefix = f"{modifier} " if modifier else ""
                    queries.append(
                        prefix + template.format(
                            practice_area=practice_area,
                            city=city,
                            state_code=state_code,
                        )
                    )
                    if len(queries) >= target_query_count:
                        return queries
        return queries

    async def _search_text(self, session: aiohttp.ClientSession, query: str) -> List[Dict[str, Any]]:
        if not self.api_key:
            raise ValueError("GOOGLE_PLACES_API_KEY is not set")
        endpoint = self.config["google_api"]["endpoint"]
        max_pages = int(self.config["google_api"]["max_pages_per_query"])
        results: List[Dict[str, Any]] = []
        next_page_token: Optional[str] = None

        for page_index in range(max_pages):
            await self.rate_limiter.acquire()
            params = {"query": query, "key": self.api_key}
            if next_page_token:
                params["pagetoken"] = next_page_token
                # Google next_page_token requires a short delay to activate.
                await asyncio.sleep(2)

            async with session.get(endpoint, params=params) as response:
                payload = await response.json()
                status = payload.get("status")
                if status not in ("OK", "ZERO_RESULTS"):
                    logger.warning("Google text search status not OK", status=status, query=query, page=page_index + 1)
                    break
                if status == "ZERO_RESULTS":
                    break
                results.extend(payload.get("results", []))
                next_page_token = payload.get("next_page_token")
                self.stats["queries_executed"] += 1
                if not next_page_token:
                    break
        return results

    async def _discover_state(self, state_code: str) -> List[Dict[str, Any]]:
        state_quota = int(self.config["states"].get(state_code, 0))
        if state_quota <= 0:
            return []
        cities = self._fetch_cities(state_code)
        all_candidates: List[Dict[str, Any]] = []

        # Early-stop ceiling: when --limit is set, gather only enough raw
        # candidates to realistically yield `limit` qualified individuals.
        # Raw candidates include firms/low-rated noise (only ~1 in 4 passes the
        # individual + threshold filters), so we target ~4x the limit.
        if self.limit is not None and self.limit >= 0:
            early_stop_ceiling = max(self.limit * 4, 40)
        else:
            early_stop_ceiling = max(state_quota * 4, 500)

        async with aiohttp.ClientSession() as session:
            for city in cities:
                city_name = city["name"]
                queries = self._build_queries_for_city(city_name, state_code, int(city["population"]))
                for query in queries:
                    places = await self._search_text(session, query)
                    for place in places:
                        all_candidates.append(
                            {
                                "google_place_id": place.get("place_id"),
                                "name": place.get("name"),
                                "formatted_address": place.get("formatted_address"),
                                "city": city_name,
                                "state": state_code,
                                "google_rating": place.get("rating"),
                                "google_review_count": place.get("user_ratings_total"),
                                "geo_lat": (place.get("geometry", {}).get("location", {}) or {}).get("lat"),
                                "geo_lng": (place.get("geometry", {}).get("location", {}) or {}).get("lng"),
                                "practice_area_searched": query.split(" lawyer")[0].split(" attorney")[0],
                                "query": query,
                                "types": place.get("types", []),
                                "source": "google_discovery",
                            }
                        )
                    if len(all_candidates) >= early_stop_ceiling:
                        break
                if len(all_candidates) >= early_stop_ceiling:
                    break
        return all_candidates

    def _persist_selected(self, selected_rows: List[Dict[str, Any]]) -> int:
        if self.dry_run or not selected_rows:
            return 0
        conn = self._scraper_conn()
        try:
            with conn.cursor() as cursor:
                for row in selected_rows:
                    merged_data = {
                        "full_name": row.get("name"),
                        "first_name": None,
                        "last_name": None,
                        "address": {
                            "line1": None,
                            "city": row.get("city"),
                            "state": row.get("state"),
                            "zip": None,
                        },
                        "google_place_id": row.get("google_place_id"),
                        "google_rating": row.get("google_rating"),
                        "google_review_count": row.get("google_review_count"),
                        "geo_lat": row.get("geo_lat"),
                        "geo_lng": row.get("geo_lng"),
                        "google_discovery_rank": row.get("google_discovery_rank"),
                        "google_popularity_score": row.get("google_popularity_score"),
                        "google_discovery_selected": True,
                        "sources": ["google_discovery"],
                    }
                    raw = {
                        "google_discovery": {
                            "discovered_at": datetime.utcnow().isoformat(),
                            "query": row.get("query"),
                            "data": row,
                        }
                    }
                    cursor.execute(
                        """
                        INSERT INTO lawyer_enrichment (
                            fingerprint,
                            enrichment_layers,
                            enrichment_level,
                            raw_data_by_source,
                            merged_data,
                            completeness_score,
                            full_name,
                            city,
                            state,
                            license_state,
                            updated_at
                        ) VALUES (
                            %s,
                            %s::jsonb,
                            3,
                            %s::jsonb,
                            %s::jsonb,
                            %s,
                            %s,
                            %s,
                            %s,
                            %s,
                            NOW()
                        )
                        ON CONFLICT (fingerprint) DO UPDATE SET
                            raw_data_by_source = lawyer_enrichment.raw_data_by_source || EXCLUDED.raw_data_by_source,
                            merged_data = lawyer_enrichment.merged_data || EXCLUDED.merged_data,
                            completeness_score = GREATEST(lawyer_enrichment.completeness_score, EXCLUDED.completeness_score),
                            full_name = COALESCE(lawyer_enrichment.full_name, EXCLUDED.full_name),
                            city = COALESCE(lawyer_enrichment.city, EXCLUDED.city),
                            state = COALESCE(lawyer_enrichment.state, EXCLUDED.state),
                            license_state = COALESCE(lawyer_enrichment.license_state, EXCLUDED.license_state),
                            updated_at = NOW()
                        """,
                        (
                            row["google_discovery_fingerprint"],
                            Json(["google_discovery"]),
                            Json(raw),
                            Json(merged_data),
                            min(100, int((row.get("google_popularity_score") or 0) * 0.9)),
                            row.get("name"),
                            row.get("city"),
                            row.get("state"),
                            row.get("state"),
                        ),
                    )
            conn.commit()
            return len(selected_rows)
        finally:
            conn.close()

    async def run(self) -> Dict[str, Any]:
        if not self.dry_run:
            try:
                self.cost_protection.enforce_batch_limit(self.batch_size)
            except CostProtectionError as exc:
                logger.error("Discovery blocked by cost protection", error=str(exc))
                raise

        all_candidates: List[Dict[str, Any]] = []
        for state_code in self.target_states:
            state_candidates = await self._discover_state(state_code)
            all_candidates.extend(state_candidates)
            self.stats["states_processed"] += 1

        self.stats["raw_candidates"] = len(all_candidates)
        individual_result = filter_individuals(all_candidates)
        self.stats["individual_candidates"] = len(individual_result.kept)
        selected = dedupe_and_rank(
            individual_result.kept,
            state_quotas={k.upper(): int(v) for k, v in self.config.get("states", {}).items()},
            min_rating=float(self.config["thresholds"]["min_google_rating"]),
            min_review_count=int(self.config["thresholds"]["min_google_review_count"]),
            min_popularity_score=float(self.config["thresholds"]["min_popularity_score_normalized"]),
        )
        # Apply an optional hard cap. `selected` is already ranked by
        # popularity_score descending, so slicing keeps the best-reviewed first.
        if self.limit is not None and self.limit >= 0:
            selected = selected[: self.limit]
        self.stats["selected_profiles"] = len(selected)
        self.stats["persisted_profiles"] = self._persist_selected(selected)
        return self.stats


async def main():
    import argparse

    parser = argparse.ArgumentParser(description="Google Discovery Worker")
    parser.add_argument("--states", nargs="*", help="State codes (e.g. CA NY TX)")
    parser.add_argument("--batch-size", type=int, default=200)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Hard cap on persisted profiles. Keeps the highest-popularity (best-reviewed) first.",
    )
    args = parser.parse_args()

    worker = GoogleDiscoveryWorker(
        target_states=args.states,
        batch_size=args.batch_size,
        dry_run=args.dry_run,
        limit=args.limit,
    )
    stats = await worker.run()
    logger.info("Google discovery complete", **stats)


if __name__ == "__main__":
    asyncio.run(main())
