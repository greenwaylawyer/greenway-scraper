"""Google-first MVP publish gate validation."""

from __future__ import annotations

import os
from typing import Any, Dict, Optional, Tuple

import psycopg2
from psycopg2.extras import RealDictCursor

from normalizers.name import NameNormalizer


class MVPPublishGate:
    """Applies publish eligibility rules to enrichment profiles."""

    def __init__(self, min_google_rating: float = 3.5, min_completeness: int = 60):
        self.min_google_rating = min_google_rating
        self.min_completeness = min_completeness

    def _conn(self):
        return psycopg2.connect(
            host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
            port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
            database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
            user=os.getenv("SCRAPER_DB_USER", "scraper"),
            password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
        )

    def _validate(self, row: Dict[str, Any]) -> Tuple[bool, Optional[str]]:
        merged = row.get("merged_data") or {}
        full_name = row.get("full_name") or merged.get("full_name")
        parsed = NameNormalizer.parse(full_name)
        if not parsed or not parsed.first_name or not parsed.last_name:
            return False, "missing_first_or_last_name"

        practice_areas = merged.get("practice_areas") or []
        if not isinstance(practice_areas, list) or len(practice_areas) < 1:
            return False, "missing_practice_areas"

        address = merged.get("address") or {}
        city = row.get("city") or address.get("city")
        state = row.get("state") or address.get("state")
        if not city or not state:
            return False, "missing_city_or_state"

        if row.get("google_rating") is not None and float(row["google_rating"]) < self.min_google_rating:
            return False, "google_rating_below_threshold"

        if int(row.get("completeness_score") or 0) < self.min_completeness:
            return False, "completeness_below_threshold"

        source_keys = set((row.get("raw_data_by_source") or {}).keys())
        if not source_keys.intersection({"google_discovery", "justia", "avvo", "google_maps"}):
            return False, "no_supported_sources"

        return True, None

    def apply(self, limit: Optional[int] = None, dry_run: bool = False) -> Dict[str, int]:
        stats = {"checked": 0, "eligible": 0, "manual_review": 0}
        conn = self._conn()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                query = """
                    SELECT id, full_name, city, state, completeness_score,
                           google_rating, merged_data, raw_data_by_source
                    FROM lawyer_enrichment
                    WHERE promoted_at IS NULL
                    ORDER BY updated_at DESC
                """
                if limit:
                    query += " LIMIT %s"
                    cursor.execute(query, (limit,))
                else:
                    cursor.execute(query)
                rows = [dict(r) for r in cursor.fetchall()]

                for row in rows:
                    stats["checked"] += 1
                    is_valid, reason = self._validate(row)
                    if is_valid:
                        stats["eligible"] += 1
                        if not dry_run:
                            cursor.execute(
                                """
                                UPDATE lawyer_enrichment
                                SET ready_for_promotion = true,
                                    manual_review_required = false,
                                    manual_review_reason = NULL,
                                    promotion_blocked = false,
                                    promotion_blocked_reason = NULL,
                                    updated_at = NOW()
                                WHERE id = %s
                                """,
                                (row["id"],),
                            )
                    else:
                        stats["manual_review"] += 1
                        if not dry_run:
                            cursor.execute(
                                """
                                UPDATE lawyer_enrichment
                                SET ready_for_promotion = false,
                                    manual_review_required = true,
                                    manual_review_reason = %s,
                                    promotion_blocked = true,
                                    promotion_blocked_reason = %s,
                                    updated_at = NOW()
                                WHERE id = %s
                                """,
                                (reason, reason, row["id"]),
                            )
            if not dry_run:
                conn.commit()
            return stats
        finally:
            conn.close()
