"""Seed layer-3 enrichment requests from Google discovery-selected profiles."""

from __future__ import annotations

import os
from typing import Optional

import psycopg2
from dotenv import load_dotenv

load_dotenv(override=True)


def _conn():
    return psycopg2.connect(
        host=os.getenv("SCRAPER_DB_HOST", "127.0.0.1"),
        port=int(os.getenv("SCRAPER_DB_PORT", 5433)),
        database=os.getenv("SCRAPER_DB_NAME", "greenway_scraper"),
        user=os.getenv("SCRAPER_DB_USER", "scraper"),
        password=os.getenv("SCRAPER_DB_PASSWORD", "scraper_secret"),
    )


def seed_requests(limit: Optional[int] = None) -> int:
    conn = _conn()
    inserted = 0
    try:
        with conn.cursor() as cursor:
            query = """
                SELECT id
                FROM lawyer_enrichment
                WHERE COALESCE((merged_data->>'google_discovery_selected')::boolean, false) = true
                ORDER BY COALESCE((merged_data->>'google_popularity_score')::numeric, 0) DESC NULLS LAST, updated_at DESC
            """
            if limit:
                query += " LIMIT %s"
                cursor.execute(query, (limit,))
            else:
                cursor.execute(query)
            ids = [row[0] for row in cursor.fetchall()]

            for lawyer_enrichment_id in ids:
                for source_key in ("justia", "avvo"):
                    cursor.execute(
                        """
                        INSERT INTO enrichment_source_requests (
                            lawyer_enrichment_id,
                            source_key,
                            layer,
                            discovery_status,
                            scrape_status,
                            requested_by,
                            priority,
                            created_at,
                            updated_at
                        ) VALUES (%s, %s, 3, 'pending', 'pending', 'google_seed', 1, NOW(), NOW())
                        ON CONFLICT (lawyer_enrichment_id, source_key) DO UPDATE SET
                            discovery_status = 'pending',
                            scrape_status = 'pending',
                            priority = GREATEST(enrichment_source_requests.priority, 1),
                            updated_at = NOW()
                        """,
                        (lawyer_enrichment_id, source_key),
                    )
                    inserted += 1
        conn.commit()
        return inserted
    finally:
        conn.close()


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Seed layer-3 requests from Google discovery selections")
    parser.add_argument("--limit", type=int, default=None)
    args = parser.parse_args()
    count = seed_requests(limit=args.limit)
    print(f"Seeded/updated {count} requests")
