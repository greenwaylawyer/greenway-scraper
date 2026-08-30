"""Merge duplicate lawyer_enrichment profiles.

Duplicate profiles arise when different sources (Google discovery, Apify Avvo,
manual entry) create separate rows for the same person with different
fingerprints. This script detects duplicates by (first name, last name, state)
and merges the lower-quality rows into the best one.

Memory-safe: duplicate detection runs on lightweight identity columns only;
full JSONB payloads are loaded solely for the (small) duplicate groups.

Usage:
    python scripts/merge_duplicate_profiles.py --dry-run   # report only
    python scripts/merge_duplicate_profiles.py             # actually merge
"""

from __future__ import annotations

import argparse
import os
import sys
from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

import psycopg2
from psycopg2.extras import Json, RealDictCursor
from dotenv import load_dotenv

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from normalizers.name import NameNormalizer
from pipeline.merge_engine import MergeEngine
from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv(override=True)

FULL_COLUMNS = (
    "id, fingerprint, full_name, city, state, license_state, "
    "completeness_score, enrichment_layers, merged_data, raw_data_by_source, "
    "google_place_id, google_rating, google_review_count, geo_lat, geo_lng"
)


def _conn():
    return psycopg2.connect(
        host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
        database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
        user=os.getenv("SCRAPER_DB_USER", "scraper"),
        password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
    )


def name_key(full_name: Optional[str], state: Optional[str], city: Optional[str]) -> Optional[Tuple[str, str, str, str]]:
    """Return a canonical (first, last, state, city) key or None if unparseable.

    City is included (and required) so distinct people who share a common name
    in the same state are not merged. Rows without a city are never merged.
    """
    if not full_name:
        return None
    parsed = NameNormalizer.parse(full_name)
    if not parsed:
        return None
    first = (parsed.first_name or "").lower().strip()
    last = (parsed.last_name or "").lower().strip()
    if not first or not last:
        return None
    st = (state or "").strip().lower()
    ct = (city or "").strip().lower()
    if not ct:
        return None
    return (first, last, st, ct)


def _union_list(a: List[Any], b: List[Any]) -> List[Any]:
    out = list(a or [])
    for item in b or []:
        if item not in out:
            out.append(item)
    return out


def deep_merge(winner: Dict[str, Any], loser: Dict[str, Any]) -> Dict[str, Any]:
    """Merge loser's merged_data into winner (winner wins conflicts)."""
    result = dict(winner or {})
    for key, value in (loser or {}).items():
        if key not in result or result[key] in (None, "", []):
            result[key] = value
        elif key in ("_field_sources", "_conflicts") and isinstance(result[key], dict) and isinstance(value, dict):
            merged = dict(result[key])
            for k, v in value.items():
                merged.setdefault(k, v)
            result[key] = merged
        elif key == "_needs_review" and isinstance(result[key], list) and isinstance(value, list):
            result[key] = _union_list(result[key], value)
        elif isinstance(result[key], list) and isinstance(value, list):
            result[key] = _union_list(result[key], value)
        elif isinstance(result[key], dict) and isinstance(value, dict):
            for k, v in value.items():
                if result[key].get(k) in (None, "", []):
                    result[key][k] = v
        # else: keep winner's scalar value
    return result


def fetch_lightweight() -> List[Dict[str, Any]]:
    """Fetch identity columns only — keeps memory bounded at ~374k profiles."""
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                """
                SELECT id, full_name, state, license_state, city, completeness_score
                FROM lawyer_enrichment
                WHERE full_name IS NOT NULL
                """
            )
            return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def fetch_members(ids: List[int]) -> List[Dict[str, Any]]:
    """Fetch full rows (with JSONB) for a small set of ids."""
    if not ids:
        return []
    conn = _conn()
    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            cursor.execute(
                f"SELECT {FULL_COLUMNS} FROM lawyer_enrichment WHERE id = ANY(%s)",
                (ids,),
            )
            return [dict(r) for r in cursor.fetchall()]
    finally:
        conn.close()


def group_duplicates(light_rows: List[Dict[str, Any]]) -> List[List[int]]:
    """Return lists of duplicate id-groups."""
    groups: Dict[Tuple, List[Dict[str, Any]]] = defaultdict(list)
    for p in light_rows:
        key = name_key(
            p.get("full_name"),
            p.get("state") or p.get("license_state"),
            p.get("city"),
        )
        if key:
            groups[key].append(p)
    return [[p["id"] for p in g] for g in groups.values() if len(g) > 1]


def pick_winner(group: List[Dict[str, Any]]) -> Dict[str, Any]:
    return sorted(
        group,
        key=lambda p: (
            p.get("completeness_score") or 0,
            len(p.get("enrichment_layers") or []),
            -(p.get("id") or 0),
        ),
        reverse=True,
    )[0]


def merge_group(winner: Dict[str, Any], loser: Dict[str, Any], conn):
    winner_id = winner["id"]
    loser_id = loser["id"]

    raw = dict(winner.get("raw_data_by_source") or {})
    for source_key, payload in (loser.get("raw_data_by_source") or {}).items():
        raw.setdefault(source_key, payload)

    merged = deep_merge(winner.get("merged_data") or {}, loser.get("merged_data") or {})
    score = MergeEngine().calculate_completeness_score(merged)

    layers = sorted(set(winner.get("enrichment_layers") or []) | set(loser.get("enrichment_layers") or []))

    google_cols = {
        "google_place_id": winner.get("google_place_id") or loser.get("google_place_id"),
        "google_rating": winner.get("google_rating") or loser.get("google_rating"),
        "google_review_count": winner.get("google_review_count") or loser.get("google_review_count"),
        "geo_lat": winner.get("geo_lat") or loser.get("geo_lat"),
        "geo_lng": winner.get("geo_lng") or loser.get("geo_lng"),
    }

    with conn.cursor(cursor_factory=RealDictCursor) as cursor:
        cursor.execute(
            "SELECT source_key FROM enrichment_source_requests WHERE lawyer_enrichment_id = %s",
            (winner_id,),
        )
        winner_sources = {r["source_key"] for r in cursor.fetchall()}

        cursor.execute(
            "SELECT id, source_key FROM enrichment_source_requests WHERE lawyer_enrichment_id = %s",
            (loser_id,),
        )
        for req in cursor.fetchall():
            if req["source_key"] in winner_sources:
                cursor.execute("DELETE FROM enrichment_source_requests WHERE id = %s", (req["id"],))
            else:
                cursor.execute(
                    "UPDATE enrichment_source_requests SET lawyer_enrichment_id = %s WHERE id = %s",
                    (winner_id, req["id"]),
                )

        cursor.execute(
            "UPDATE promotion_log SET lawyer_enrichment_id = %s WHERE lawyer_enrichment_id = %s",
            (winner_id, loser_id),
        )
        cursor.execute(
            "UPDATE enrichment_errors SET lawyer_id = %s WHERE lawyer_id = %s",
            (winner_id, loser_id),
        )

        cursor.execute(
            """
            UPDATE lawyer_enrichment
            SET merged_data = %s,
                raw_data_by_source = %s,
                enrichment_layers = %s::jsonb,
                completeness_score = %s,
                google_place_id = COALESCE(google_place_id, %s),
                google_rating = COALESCE(google_rating, %s),
                google_review_count = COALESCE(google_review_count, %s),
                geo_lat = COALESCE(geo_lat, %s),
                geo_lng = COALESCE(geo_lng, %s),
                city = COALESCE(city, (SELECT city FROM lawyer_enrichment WHERE id = %s)),
                state = COALESCE(state, (SELECT state FROM lawyer_enrichment WHERE id = %s)),
                license_state = COALESCE(license_state, (SELECT license_state FROM lawyer_enrichment WHERE id = %s)),
                updated_at = NOW()
            WHERE id = %s
            """,
            (
                Json(merged),
                Json(raw),
                Json(layers),
                score,
                google_cols["google_place_id"],
                google_cols["google_rating"],
                google_cols["google_review_count"],
                google_cols["geo_lat"],
                google_cols["geo_lng"],
                loser_id,
                loser_id,
                loser_id,
                winner_id,
            ),
        )

        cursor.execute("DELETE FROM lawyer_enrichment WHERE id = %s", (loser_id,))


def run(dry_run: bool) -> Dict[str, int]:
    light_rows = fetch_lightweight()
    logger.info("Loaded profiles", total=len(light_rows))

    dup_groups = group_duplicates(light_rows)
    stats = {"groups": len(dup_groups), "duplicates": 0, "merged": 0}
    if not dup_groups:
        logger.info("No duplicate profiles found")
        return stats

    all_ids = [i for g in dup_groups for i in g]
    members = {r["id"]: r for r in fetch_members(all_ids)}

    conn = _conn()
    try:
        for id_group in dup_groups:
            group = [members[i] for i in id_group]
            winner = pick_winner(group)
            losers = [p for p in group if p["id"] != winner["id"]]
            stats["duplicates"] += len(losers)
            logger.info(
                "Duplicate group",
                winner=f"{winner['full_name']} (#{winner['id']}, score={winner['completeness_score']})",
                losers=[f"{p['full_name']} (#{p['id']}, score={p['completeness_score']})" for p in losers],
            )
            for loser in losers:
                if not dry_run:
                    merge_group(winner, loser, conn)
                stats["merged"] += 1

            if not dry_run:
                conn.commit()
    finally:
        conn.close()
    return stats


def main():
    parser = argparse.ArgumentParser(description="Merge duplicate lawyer profiles")
    parser.add_argument("--dry-run", action="store_true", help="Report duplicates without merging")
    args = parser.parse_args()

    stats = run(dry_run=args.dry_run)
    logger.info(
        "Duplicate merge complete" if not args.dry_run else "Duplicate scan complete (dry-run)",
        **stats,
    )


if __name__ == "__main__":
    main()
