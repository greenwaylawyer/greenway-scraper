#!/usr/bin/env python3
"""Fix duplicate bar_number entries and add unique constraint."""

import psycopg2
import os
from dotenv import load_dotenv

load_dotenv(override=True)

conn = psycopg2.connect(
    host=os.getenv('SCRAPER_DB_HOST', '127.0.0.1'),
    port=int(os.getenv('SCRAPER_DB_PORT', 5433)),
    database=os.getenv('SCRAPER_DB_NAME', 'greenway_scraper'),
    user=os.getenv('SCRAPER_DB_USER', 'scraper'),
    password=os.getenv('SCRAPER_DB_PASSWORD', 'scraper_secret'),
)
cur = conn.cursor()

# 1. Check total records
cur.execute('SELECT COUNT(*) FROM lawyer_enrichment')
total = cur.fetchone()[0]
print(f"Total records: {total}")

# 2. Find duplicates by bar_number + license_state
cur.execute("""
    SELECT bar_number, license_state, COUNT(*) as cnt
    FROM lawyer_enrichment
    WHERE bar_number IS NOT NULL
    GROUP BY bar_number, license_state
    HAVING COUNT(*) > 1
    ORDER BY cnt DESC
""")
dupes = cur.fetchall()
print(f"Duplicate bar_number groups: {len(dupes)}")
for d in dupes[:10]:
    print(f"  bar={d[0]} state={d[1]} count={d[2]}")

if not dupes:
    print("\nNo duplicates found!")
else:
    # 3. Delete duplicates - keep the row with highest enrichment_level (or latest updated_at)
    print(f"\nDeleting duplicates (keeping best record per bar_number+state)...")
    cur.execute("""
        DELETE FROM lawyer_enrichment
        WHERE id NOT IN (
            SELECT DISTINCT ON (bar_number, license_state) id
            FROM lawyer_enrichment
            WHERE bar_number IS NOT NULL
            ORDER BY bar_number, license_state, enrichment_level DESC, completeness_score DESC, updated_at DESC
        )
        AND bar_number IS NOT NULL
    """)
    deleted = cur.rowcount
    print(f"Deleted {deleted} duplicate rows")
    conn.commit()

# 4. Check records after cleanup
cur.execute('SELECT COUNT(*) FROM lawyer_enrichment')
total_after = cur.fetchone()[0]
print(f"\nRecords after cleanup: {total_after}")

# 5. Drop old fingerprint unique constraint and add bar_number unique constraint
print("\nUpdating constraints...")

# Check existing constraints
cur.execute("""
    SELECT conname, contype FROM pg_constraint
    WHERE conrelid = 'lawyer_enrichment'::regclass
""")
constraints = cur.fetchall()
print(f"Existing constraints: {constraints}")

# Drop fingerprint unique index if exists
try:
    cur.execute("DROP INDEX IF EXISTS lawyer_enrichment_fingerprint_key")
    print("Dropped old fingerprint unique index")
except Exception as e:
    print(f"Note: {e}")
    conn.rollback()

# Add unique constraint on bar_number + license_state
try:
    cur.execute("""
        CREATE UNIQUE INDEX IF NOT EXISTS idx_unique_bar_number_state
        ON lawyer_enrichment (bar_number, license_state)
        WHERE bar_number IS NOT NULL
    """)
    conn.commit()
    print("Added UNIQUE index on (bar_number, license_state)")
except Exception as e:
    print(f"Failed to add unique index: {e}")
    conn.rollback()

# 6. Verify
cur.execute("""
    SELECT indexname, indexdef FROM pg_indexes
    WHERE tablename = 'lawyer_enrichment'
    ORDER BY indexname
""")
print("\nFinal indexes:")
for idx in cur.fetchall():
    print(f"  {idx[0]}")

conn.close()
print("\nDone!")
