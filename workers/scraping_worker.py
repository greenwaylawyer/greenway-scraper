"""
Scraping Worker for Layer 3 & 4 enrichment.

Polls enrichment_source_requests where scrape_status IN ('pending', 'queued')
and source_profile_url IS NOT NULL, then runs the source-specific parser.
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import get_source_config
from scrapers.enrichers.base_enricher import BaseEnricher
from pipeline.merge_engine import merge_scraped_data
from utils.logger import get_logger
from utils.rate_limiter import TokenBucketRateLimiter

logger = get_logger(__name__)
load_dotenv(override=True)


class ScrapingWorker:
    """
    Worker that processes scraping requests for enrichment sources.
    
    1. Polls enrichment_source_requests WHERE scrape_status IN ('pending', 'queued')
       AND source_profile_url IS NOT NULL
    2. Loads appropriate enricher class
    3. Calls enricher.parse_profile(url)
    4. Stores scraped_data in request
    5. Merges into lawyer_enrichment.merged_data
    6. Updates completeness_score
    7. Sets scrape_status='completed'
    """

    def __init__(
        self,
        source_key: Optional[str] = None,
        batch_size: int = 10,
        max_attempts: int = 3,
    ):
        """
        Initialize scraping worker.
        
        Args:
            source_key: Process only this source (None = all enabled sources)
            batch_size: Number of requests to process per poll cycle
            max_attempts: Maximum scrape attempts before marking failed
        """
        self.source_key = source_key
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.enrichers = {}  # Cache enricher instances
        self.rate_limiters = {}  # Rate limiters per source
        
        # Stats
        self.stats = {
            'total_processed': 0,
            'completed': 0,
            'failed': 0,
            'no_data': 0,
            'errors': 0,
        }

    def _get_db_connection(self):
        """Create database connection to scraper database."""
        return psycopg2.connect(
            host=os.getenv('SCRAPER_DB_HOST', '127.0.0.1'),
            port=int(os.getenv('SCRAPER_DB_PORT', 5433)),
            database=os.getenv('SCRAPER_DB_NAME', 'greenway_scraper'),
            user=os.getenv('SCRAPER_DB_USER', 'scraper'),
            password=os.getenv('SCRAPER_DB_PASSWORD', 'scraper_secret'),
        )

    def _fetch_current_merged_data(self, lawyer_enrichment_id: int) -> Dict[str, Any]:
        """Re-read merged_data from DB immediately before merge to avoid stale batch data."""
        conn = self._get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                cursor.execute(
                    "SELECT merged_data FROM lawyer_enrichment WHERE id = %s",
                    (lawyer_enrichment_id,),
                )
                row = cursor.fetchone()
                return row['merged_data'] if row and row['merged_data'] else {}
        finally:
            conn.close()

    def get_enricher(self, source_key: str) -> Optional[BaseEnricher]:
        """Get or create enricher instance for a source."""
        if source_key in self.enrichers:
            return self.enrichers[source_key]
        
        config = get_source_config(source_key)
        if not config:
            logger.error(f"No config found for source: {source_key}")
            return None
        
        # Dynamic import
        module_path = config.get('scraper_module') or config.get('api_module')
        class_name = config.get('scraper_class') or config.get('api_class')
        
        if not module_path or not class_name:
            logger.error(f"Missing module/class config for {source_key}")
            return None
        
        try:
            module = __import__(module_path, fromlist=[class_name])
            enricher_class = getattr(module, class_name)
            enricher = enricher_class()
            self.enrichers[source_key] = enricher
            return enricher
        except (ImportError, AttributeError) as e:
            logger.error(f"Failed to load enricher for {source_key}: {e}")
            return None

    def get_rate_limiter(self, source_key: str) -> TokenBucketRateLimiter:
        """Get or create rate limiter for a source."""
        if source_key not in self.rate_limiters:
            config = get_source_config(source_key)
            rate = config.get('rate_limit', 10) if config else 10
            self.rate_limiters[source_key] = TokenBucketRateLimiter(rate=rate)
        return self.rate_limiters[source_key]

    def fetch_pending_requests(self, limit: int) -> List[Dict[str, Any]]:
        """
        Fetch pending scrape requests from database.
        
        Returns requests ordered by priority DESC, created_at ASC.
        """
        conn = self._get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                query = """
                    SELECT 
                        esr.id AS request_id,
                        esr.lawyer_enrichment_id,
                        esr.source_key,
                        esr.layer,
                        esr.source_profile_url,
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
                    WHERE esr.scrape_status IN ('pending', 'queued')
                      AND esr.source_profile_url IS NOT NULL
                      AND esr.scrape_attempts < %s
                """
                params = [self.max_attempts]
                
                if self.source_key:
                    query += " AND esr.source_key = %s"
                    params.append(self.source_key)
                
                query += """
                    ORDER BY esr.priority DESC, esr.created_at ASC
                    LIMIT %s
                """
                params.append(limit)
                
                cursor.execute(query, params)
                return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    async def process_request(self, request: Dict[str, Any]) -> bool:
        """
        Process a single scraping request.
        
        Returns:
            True if successful, False if error
        """
        request_id = request['request_id']
        source_key = request['source_key']
        url = request['source_profile_url']
        
        logger.info(
            f"Processing scrape request {request_id}",
            source=source_key,
            lawyer=request['full_name'],
            url=url,
        )
        
        try:
            # Get enricher
            enricher = self.get_enricher(source_key)
            if not enricher:
                await self._update_request_error(
                    request_id,
                    f"Enricher not available for {source_key}",
                )
                self.stats['errors'] += 1
                return False
            
            # Rate limiting
            rate_limiter = self.get_rate_limiter(source_key)
            await rate_limiter.acquire()
            
            # Update status to 'scraping'
            await self._update_status_scraping(request_id)
            
            # Parse profile
            lawyer_data = {
                'full_name': request['full_name'],
                'first_name': request['first_name'],
                'last_name': request['last_name'],
                'bar_number': request.get('bar_number'),
                'state': request.get('state'),
                'city': request.get('city'),
                'firm_name': request.get('firm_name'),
            }
            
            scraped_data = await enricher.parse_profile(url, lawyer_data)
            
            if scraped_data is None or not scraped_data:
                # No data extracted
                await self._update_request_no_data(request_id)
                self.stats['no_data'] += 1
                logger.warning(f"No data found at {url} for request {request_id}")
                return True

            if set(scraped_data.keys()) == {'source_profile_url'}:
                # Scraper returned only the URL — no profile data extracted
                await self._update_request_no_data(request_id)
                self.stats['no_data'] += 1
                logger.warning(f"Only source_profile_url from {url} — treating as no_data")
                return True
            
            # Merge into lawyer_enrichment.
            # Re-read merged_data from DB to avoid stale data from a batch fetch
            # where another request for the same profile was processed earlier in
            # this poll cycle.
            fresh_merged = self._fetch_current_merged_data(request['lawyer_enrichment_id'])

            merge_result = merge_scraped_data(
                lawyer_enrichment={
                    'merged_data': fresh_merged,
                    'manually_curated_fields': request.get('manually_curated_fields', []),
                },
                scraped_data=scraped_data,
                source_key=source_key,
                layer=request['layer'],
            )
            
            # Update request and lawyer_enrichment
            await self._update_request_completed(
                request_id=request_id,
                lawyer_enrichment_id=request['lawyer_enrichment_id'],
                scraped_data=scraped_data,
                merged_data=merge_result['merged_data'],
                completeness_score=merge_result['completeness_score'],
                source_key=source_key,
                layer=request['layer'],
            )

            # Sync resolved identity/location from merged_data into the
            # top-level lawyer_enrichment columns (full_name, first_name, etc.)
            # so the Curate page, list views, and promotion see them without
            # having to read merged_data. Only fills when present + not already
            # set on the column (fill-if-empty semantics).
            await self._sync_identity_columns(
                request['lawyer_enrichment_id'],
                merge_result['merged_data'],
            )
            
            self.stats['completed'] += 1
            logger.info(
                f"Successfully scraped request {request_id}",
                fields_merged=merge_result['merge_summary']['fields_merged'],
                conflicts=merge_result['merge_summary']['conflicts_detected'],
                new_score=merge_result['completeness_score'],
            )
            return True
            
        except Exception as e:
            logger.error(
                f"Scraping failed for request {request_id}: {e}",
                exc_info=True,
            )
            await self._update_request_error(request_id, str(e))
            self.stats['errors'] += 1
            return False

    async def _update_status_scraping(self, request_id: int):
        """Update request status to 'scraping'."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET scrape_status = 'scraping',
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (request_id,),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_completed(
        self,
        request_id: int,
        lawyer_enrichment_id: int,
        scraped_data: Dict[str, Any],
        merged_data: Dict[str, Any],
        completeness_score: int,
        source_key: str,
        layer: int,
    ):
        """Update request as completed, merge into lawyer_enrichment, advance enrichment_level."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                # 1. Mark the source request as completed
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET scrape_status = 'completed',
                        scraped_data = %s,
                        scraped_at = NOW(),
                        merged_to_profile = true,
                        merged_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (psycopg2.extras.Json(scraped_data), request_id),
                )

                # 2. Merge into lawyer_enrichment, append raw_data_by_source,
                #    advance enrichment_level, and update ready_for_promotion.
                threshold = int(os.getenv("COMPLETENESS_THRESHOLD", 80))
                cursor.execute(
                    """
                    UPDATE lawyer_enrichment
                    SET merged_data = %s,
                        completeness_score = %s,
                        raw_data_by_source = raw_data_by_source || jsonb_build_object(
                            %s,
                            jsonb_build_object(
                                'scraped_at', %s,
                                'layer', %s,
                                'source_url', (SELECT source_profile_url FROM enrichment_source_requests WHERE id = %s),
                                'data', (%s)::jsonb
                            )
                        ),
                        enrichment_layers = CASE
                            WHEN NOT (enrichment_layers @> jsonb_build_array(%s))
                            THEN enrichment_layers || jsonb_build_array(%s)
                            ELSE enrichment_layers
                        END,
                        enrichment_level = CASE
                            WHEN enrichment_level < %s THEN %s
                            ELSE enrichment_level
                        END,
                        ready_for_promotion = (
                            %s >= %s
                            AND NOT COALESCE(promotion_blocked, false)
                            AND promoted_at IS NULL
                        ),
                        last_enriched_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        psycopg2.extras.Json(merged_data),
                        completeness_score,
                        source_key,
                        datetime.now().isoformat(),
                        layer,
                        request_id,
                        psycopg2.extras.Json(scraped_data),
                        source_key,
                        source_key,
                        layer,
                        layer,
                        completeness_score,  # for ready_for_promotion check
                        threshold,
                        lawyer_enrichment_id,
                    ),
                )

                conn.commit()
        finally:
            conn.close()

    async def _sync_identity_columns(self, lawyer_enrichment_id: int, merged_data: Dict[str, Any]) -> None:
        """
        Backfill top-level lawyer_enrichment identity/location columns from
        merged_data, fill-if-empty only (COALESCE keeps any existing value).

        The scraper writes enrichment fields into merged_data; these top-level
        columns are what the Curate page, lawyer list, and promotion job read.
        Address fields are read from merged_data['address'][...] when present.
        """
        if not merged_data:
            return

        address = merged_data.get('address') or {}
        values = {
            'full_name': merged_data.get('full_name'),
            'first_name': merged_data.get('first_name'),
            'last_name': merged_data.get('last_name'),
            'firm_name': merged_data.get('firm_name'),
            'bar_number': merged_data.get('bar_number'),
            'license_state': merged_data.get('license_state') or address.get('state') or merged_data.get('state'),
            'license_status': merged_data.get('license_status'),
            'admission_date': self._coerce_date(merged_data.get('admission_date')),
            'city': address.get('city') or merged_data.get('city'),
            'state': address.get('state') or merged_data.get('state'),
        }
        # Drop empties; only fill columns that have a value to set.
        values = {k: v for k, v in values.items() if v not in (None, '')}
        if not values:
            return

        # admission_date is a DATE column — pass a date object and skip the
        # NULLIF(...,'') text comparison, which would raise
        # "COALESCE types text and date cannot be matched".
        set_parts: List[str] = []
        params: List[Any] = []
        for col, val in values.items():
            if col == 'admission_date':
                set_parts.append(f"{col} = COALESCE({col}, %s)")
            else:
                set_parts.append(f"{col} = COALESCE(NULLIF(%s, ''), {col})")
            params.append(val)
        params.append(lawyer_enrichment_id)

        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    f"""
                    UPDATE lawyer_enrichment
                    SET {", ".join(set_parts)}, updated_at = NOW()
                    WHERE id = %s
                    """,
                    params,
                )
            conn.commit()
        finally:
            conn.close()

    @staticmethod
    def _coerce_date(value: Any) -> Any:
        """Coerce a merged_data admission_date value to a datetime.date (or None)."""
        if value is None or value == '':
            return None
        if isinstance(value, datetime):
            return value.date()
        if isinstance(value, str):
            text = value.strip()
            for fmt in ('%Y-%m-%d', '%Y-%m', '%Y', '%m/%d/%Y'):
                try:
                    return datetime.strptime(text, fmt).date()
                except ValueError:
                    continue
            return None
        return value

    async def _update_request_no_data(self, request_id: int):
        """Update request as no_data (page found but empty)."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET scrape_status = 'no_data',
                        scraped_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (request_id,),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_error(self, request_id: int, error: str):
        """Increment attempts, set error if max reached."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET scrape_attempts = scrape_attempts + 1,
                        scrape_error = %s,
                        scrape_status = CASE 
                            WHEN scrape_attempts + 1 >= %s THEN 'failed'
                            ELSE scrape_status
                        END,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (error, self.max_attempts, request_id),
                )
                conn.commit()
        finally:
            conn.close()

    async def run_once(self):
        """Run one poll cycle."""
        requests = self.fetch_pending_requests(self.batch_size)
        
        if not requests:
            logger.debug("No pending scrape requests")
            return
        
        logger.info(f"Processing {len(requests)} scrape requests")
        
        for request in requests:
            await self.process_request(request)
            self.stats['total_processed'] += 1
            
            # Small delay between requests
            await asyncio.sleep(0.5)
        
        logger.info(
            "Scraping cycle complete",
            **self.stats,
        )

    async def run(self, poll_interval: int = 30):
        """
        Run worker in continuous poll mode.
        
        Args:
            poll_interval: Seconds between poll cycles
        """
        logger.info(
            "Scraping worker started",
            source_key=self.source_key or "all",
            batch_size=self.batch_size,
            poll_interval=poll_interval,
        )
        
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"Scraping worker error: {e}", exc_info=True)
            
            await asyncio.sleep(poll_interval)


async def main():
    """Main entry point for standalone execution."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Scraping Worker')
    parser.add_argument('--source', help='Process only this source')
    parser.add_argument('--batch-size', type=int, default=10, help='Batch size')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--poll-interval', type=int, default=30, help='Poll interval in seconds')
    
    args = parser.parse_args()
    
    worker = ScrapingWorker(
        source_key=args.source,
        batch_size=args.batch_size,
    )
    
    if args.once:
        await worker.run_once()
        logger.info("Single run complete", **worker.stats)
    else:
        await worker.run(poll_interval=args.poll_interval)


if __name__ == '__main__':
    asyncio.run(main())
