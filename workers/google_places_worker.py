"""
Google Places Worker for Layer 4 batch enrichment.

This worker is separate from the general discovery/scraping workers because
Layer 4 has unique requirements:
- Batch processing (500 profiles at once)
- Minimum completeness threshold (60%)
- Cost control (place_id reuse, refresh intervals)
- Priority ordering (high completeness first)
"""

import asyncio
import os
import sys
from pathlib import Path
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
import uuid

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent.parent))

from config.loader import get_source_config
from scrapers.enrichers.base_enricher import BaseEnricher
from pipeline.merge_engine import merge_scraped_data
from utils.logger import get_logger
from utils.rate_limiter import TokenBucketRateLimiter
from utils.cost_protection import CostProtectionSystem, CostProtectionError

logger = get_logger(__name__)
load_dotenv(override=True)


class GooglePlacesWorker:
    """
    Specialized worker for Layer 4 (Google Maps) enrichment.
    
    Key differences from standard scraping worker:
    1. Batch processing: Creates batch_id for tracking
    2. Completeness filter: Only processes profiles with score >= min_completeness
    3. Priority ordering: High completeness profiles first
    4. Refresh control: Respects refresh_interval_days
    5. Cost tracking: Logs API usage for billing
    """

    def __init__(
        self,
        batch_size: int = 500,
        min_completeness: int = 60,
        refresh_interval_days: int = 30,
        max_attempts: int = 3,
        skip_cost_check: bool = False,
    ):
        """
        Initialize Google Places worker.
        
        Args:
            batch_size: Number of profiles to process per batch
            min_completeness: Minimum completeness score required
            refresh_interval_days: Days before re-fetching existing data
            max_attempts: Maximum scrape attempts before marking failed
            skip_cost_check: Skip cost protection checks (DANGEROUS - use with caution)
        """
        self.batch_size = batch_size
        self.min_completeness = min_completeness
        self.refresh_interval_days = refresh_interval_days
        self.max_attempts = max_attempts
        self.skip_cost_check = skip_cost_check
        
        self.source_key = 'google_maps'
        self.enricher = None
        self.rate_limiter = None
        
        # Initialize cost protection
        self.cost_protection = CostProtectionSystem()
        
        # Load source config
        self.config = get_source_config(self.source_key)
        if not self.config:
            raise ValueError(f"No config found for {self.source_key}")
        
        # Stats
        self.stats = {
            'batch_id': None,
            'total_selected': 0,
            'discovery_success': 0,
            'scrape_success': 0,
            'skipped': 0,
            'errors': 0,
            'api_calls': {
                'find_place': 0,
                'place_details': 0,
            },
            'estimated_cost_usd': 0.0,
            'pre_batch_usage': None,
            'post_batch_usage': None,
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

    def get_enricher(self) -> Optional[BaseEnricher]:
        """Get or create Google Places enricher instance."""
        if self.enricher:
            return self.enricher
        
        module_path = self.config.get('api_module')
        class_name = self.config.get('api_class')
        
        if not module_path or not class_name:
            logger.error(f"Missing module/class config for {self.source_key}")
            return None
        
        try:
            module = __import__(module_path, fromlist=[class_name])
            enricher_class = getattr(module, class_name)
            self.enricher = enricher_class()
            return self.enricher
        except (ImportError, AttributeError) as e:
            logger.error(f"Failed to load enricher for {self.source_key}: {e}")
            return None

    def get_rate_limiter(self) -> TokenBucketRateLimiter:
        """Get or create rate limiter."""
        if not self.rate_limiter:
            rate = self.config.get('rate_limit', 50)
            self.rate_limiter = TokenBucketRateLimiter(rate=rate)
        return self.rate_limiter

    def select_profiles_for_batch(self) -> List[Dict[str, Any]]:
        """
        Select profiles for Layer 4 enrichment based on criteria:
        1. Completeness score >= min_completeness
        2. Not already enriched or due for refresh
        3. High completeness first
        4. Limit to batch_size
        
        Returns:
            List of profile dicts ready for enrichment
        """
        conn = self._get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                # Calculate refresh cutoff date
                refresh_cutoff = datetime.now() - timedelta(days=self.refresh_interval_days)
                
                query = """
                    SELECT 
                        le.id AS lawyer_enrichment_id,
                        le.full_name,
                        le.first_name,
                        le.last_name,
                        le.firm_name,
                        le.city,
                        le.state AS address_state,
                        le.license_state,
                        le.merged_data,
                        le.completeness_score,
                        le.manually_curated_fields,
                        esr.id AS existing_request_id,
                        esr.place_id,
                        esr.scraped_at
                    FROM lawyer_enrichment le
                    LEFT JOIN enrichment_source_requests esr 
                        ON esr.lawyer_enrichment_id = le.id 
                        AND esr.source_key = %s
                    WHERE le.completeness_score >= %s
                      AND le.promotion_blocked = false
                      AND (
                          -- Never enriched with Google Maps
                          esr.id IS NULL
                          OR
                          -- Due for refresh
                          (esr.scrape_status = 'completed' AND esr.scraped_at < %s)
                          OR
                          -- Previous attempt failed
                          (esr.scrape_status = 'failed' AND esr.scrape_attempts < %s)
                      )
                    ORDER BY le.completeness_score DESC, le.updated_at DESC
                    LIMIT %s
                """
                
                cursor.execute(query, (
                    self.source_key,
                    self.min_completeness,
                    refresh_cutoff,
                    self.max_attempts,
                    self.batch_size,
                ))
                
                return [dict(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    async def create_batch_requests(
        self,
        profiles: List[Dict[str, Any]],
    ) -> str:
        """
        Create enrichment_source_requests for all profiles in batch.
        
        Args:
            profiles: List of selected profiles
        
        Returns:
            batch_id UUID as string
        """
        batch_id = str(uuid.uuid4())
        conn = self._get_db_connection()
        
        try:
            with conn.cursor() as cursor:
                for profile in profiles:
                    # Check if request already exists
                    if profile.get('existing_request_id'):
                        # Update existing request for refresh
                        cursor.execute(
                            """
                            UPDATE enrichment_source_requests
                            SET batch_id = %s,
                                discovery_status = 'pending',
                                scrape_status = 'pending',
                                priority = 1,
                                updated_at = NOW()
                            WHERE id = %s
                            """,
                            (batch_id, profile['existing_request_id']),
                        )
                    else:
                        # Create new request
                        cursor.execute(
                            """
                            INSERT INTO enrichment_source_requests (
                                lawyer_enrichment_id,
                                source_key,
                                layer,
                                batch_id,
                                requested_by,
                                priority,
                                created_at,
                                updated_at
                            ) VALUES (%s, %s, 4, %s, 'google_places_worker', 1, NOW(), NOW())
                            """,
                            (profile['lawyer_enrichment_id'], self.source_key, batch_id),
                        )
                
                conn.commit()
        finally:
            conn.close()
        
        return batch_id

    async def process_batch(self) -> Dict[str, Any]:
        """
        Process one batch of Google Places enrichment.
        
        Returns:
            Stats dict with results
        """
        # ========================================================================
        # COST PROTECTION CHECK
        # ========================================================================
        if not self.skip_cost_check:
            try:
                check_result = self.cost_protection.enforce_batch_limit(self.batch_size)
                
                logger.info(
                    "Cost protection check passed",
                    environment=self.cost_protection.env,
                    batch_size=self.batch_size,
                    estimated_cost=check_result.get('estimated_new_cost', 0),
                    usage=check_result.get('usage', {}),
                )
                
                # Store pre-batch usage for comparison
                self.stats['pre_batch_usage'] = check_result['usage']
                
            except CostProtectionError as e:
                logger.error(f"BATCH BLOCKED BY COST PROTECTION: {e}")
                self.stats['error'] = str(e)
                self.stats['blocked_by_cost_protection'] = True
                return self.stats
        else:
            logger.warning(
                "⚠️  COST PROTECTION DISABLED — PROCEEDING WITHOUT LIMITS",
                batch_size=self.batch_size,
            )
        
        # Select profiles
        logger.info(
            f"Selecting profiles for Layer 4 batch",
            batch_size=self.batch_size,
            min_completeness=self.min_completeness,
        )
        
        profiles = self.select_profiles_for_batch()
        self.stats['total_selected'] = len(profiles)
        
        if not profiles:
            logger.info("No profiles eligible for Layer 4 enrichment")
            return self.stats
        
        logger.info(
            f"Selected {len(profiles)} profiles for Layer 4 batch",
            avg_completeness=sum(p['completeness_score'] for p in profiles) / len(profiles),
        )
        
        # Create batch requests
        batch_id = await self.create_batch_requests(profiles)
        self.stats['batch_id'] = batch_id
        
        logger.info(f"Created batch {batch_id} with {len(profiles)} requests")
        
        # Get enricher
        enricher = self.get_enricher()
        if not enricher:
            logger.error("Google Places enricher not available")
            return self.stats
        
        rate_limiter = self.get_rate_limiter()
        
        # Process each profile
        for i, profile in enumerate(profiles, 1):
            try:
                await self._process_single_profile(
                    profile,
                    enricher,
                    rate_limiter,
                    i,
                    len(profiles),
                )
            except Exception as e:
                logger.error(
                    f"Error processing profile {profile['lawyer_enrichment_id']}: {e}",
                    exc_info=True,
                )
                self.stats['errors'] += 1
        
        # Calculate cost
        self._calculate_cost()
        
        # Get post-batch usage
        self.stats['post_batch_usage'] = self.cost_protection.get_today_usage()
        
        logger.info(
            "Layer 4 batch complete",
            **self.stats,
        )
        
        # Print usage report
        if not self.skip_cost_check:
            self.cost_protection.print_usage_report()
        
        return self.stats

    async def _process_single_profile(
        self,
        profile: Dict[str, Any],
        enricher: BaseEnricher,
        rate_limiter: TokenBucketRateLimiter,
        index: int,
        total: int,
    ):
        """Process a single profile through discovery and scraping."""
        logger.info(
            f"Processing profile {index}/{total}: {profile['full_name']}",
            completeness=profile['completeness_score'],
        )
        
        # Rate limit
        await rate_limiter.acquire()
        
        # If we have a place_id, skip discovery
        if profile.get('place_id'):
            logger.info(f"Reusing place_id for {profile['full_name']}")
            await self._scrape_profile(profile, enricher, profile['place_id'])
            self.stats['api_calls']['place_details'] += 1
            return
        
        # Discovery phase
        merged_data = profile.get('merged_data', {})
        address_data = merged_data.get('address', {})
        
        # Build address string from merged_data->address if available
        address_str = None
        if isinstance(address_data, dict):
            addr_parts = [
                address_data.get('line1', ''),
                address_data.get('city', ''),
                address_data.get('state', ''),
            ]
            address_str = ', '.join(p for p in addr_parts if p)
        
        lawyer_data = {
            'full_name': profile['full_name'],
            'first_name': profile['first_name'],
            'last_name': profile['last_name'],
            'firm_name': profile.get('firm_name'),
            'city': profile.get('city'),
            'state': profile.get('address_state') or profile.get('license_state'),
            'address': address_str or '',
            'phone': merged_data.get('phone'),
        }
        
        candidates = await enricher.discover_profile(lawyer_data)
        self.stats['api_calls']['find_place'] += 1
        
        if not candidates or len(candidates) == 0:
            logger.warning(f"No Google Maps listing found for {profile['full_name']}")
            await self._update_request_not_found(profile)
            self.stats['skipped'] += 1
            return
        
        # Take best candidate
        best = candidates[0]
        logger.info(
            f"Found Google Maps listing for {profile['full_name']}",
            confidence=best.confidence,
        )
        
        await self._update_request_found(profile, best.url, best.confidence)
        self.stats['discovery_success'] += 1
        
        # Scraping phase
        await rate_limiter.acquire()
        await self._scrape_profile(profile, enricher, best.url)
        self.stats['api_calls']['place_details'] += 1

    async def _scrape_profile(
        self,
        profile: Dict[str, Any],
        enricher: BaseEnricher,
        place_id: str,
    ):
        """Scrape and merge data from Google Places."""
        try:
            scraped_data = await enricher.parse_profile(place_id, {})
            
            if not scraped_data:
                logger.warning(f"No data returned for {profile['full_name']}")
                await self._update_request_no_data(profile)
                return
            
            # Merge into profile
            merge_result = merge_scraped_data(
                lawyer_enrichment={
                    'merged_data': profile.get('merged_data', {}),
                    'manually_curated_fields': profile.get('manually_curated_fields', []),
                },
                scraped_data=scraped_data,
                source_key=self.source_key,
                layer=4,
            )
            
            # Update database
            await self._update_request_completed(
                profile=profile,
                scraped_data=scraped_data,
                merged_data=merge_result['merged_data'],
                completeness_score=merge_result['completeness_score'],
                place_id=place_id,
            )
            
            self.stats['scrape_success'] += 1
            logger.info(
                f"Successfully enriched {profile['full_name']}",
                rating=scraped_data.get('rating'),
                review_count=scraped_data.get('review_count'),
            )
            
        except Exception as e:
            logger.error(f"Failed to scrape {profile['full_name']}: {e}", exc_info=True)
            await self._update_request_error(profile, str(e))
            self.stats['errors'] += 1

    async def _update_request_found(
        self,
        profile: Dict[str, Any],
        place_id: str,
        confidence: float,
    ):
        """Mark request as found with place_id."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET discovery_status = 'found',
                        source_profile_url = %s,
                        place_id = %s,
                        match_confidence = %s,
                        discovery_method = 'places_api_search',
                        discovery_completed_at = NOW(),
                        scrape_status = 'queued',
                        updated_at = NOW()
                    WHERE lawyer_enrichment_id = %s AND source_key = %s
                    """,
                    (place_id, place_id, confidence, profile['lawyer_enrichment_id'], self.source_key),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_not_found(self, profile: Dict[str, Any]):
        """Mark request as not found."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET discovery_status = 'not_found',
                        discovery_completed_at = NOW(),
                        updated_at = NOW()
                    WHERE lawyer_enrichment_id = %s AND source_key = %s
                    """,
                    (profile['lawyer_enrichment_id'], self.source_key),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_no_data(self, profile: Dict[str, Any]):
        """Mark request as no data."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET scrape_status = 'no_data',
                        scraped_at = NOW(),
                        updated_at = NOW()
                    WHERE lawyer_enrichment_id = %s AND source_key = %s
                    """,
                    (profile['lawyer_enrichment_id'], self.source_key),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_error(self, profile: Dict[str, Any], error: str):
        """Mark request as failed."""
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
                    WHERE lawyer_enrichment_id = %s AND source_key = %s
                    """,
                    (error, self.max_attempts, profile['lawyer_enrichment_id'], self.source_key),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_completed(
        self,
        profile: Dict[str, Any],
        scraped_data: Dict[str, Any],
        merged_data: Dict[str, Any],
        completeness_score: int,
        place_id: str,
    ):
        """Mark request as completed and update profile."""
        conn = self._get_db_connection()
        
        # Extract Google-specific fields from scraped_data
        google_rating = scraped_data.get('google_rating')
        google_review_count = scraped_data.get('google_review_count')
        geo_lat = scraped_data.get('geo_lat')
        geo_lng = scraped_data.get('geo_lng')
        
        # Determine completeness threshold
        completeness_threshold = int(os.getenv('COMPLETENESS_THRESHOLD', 80))
        
        try:
            with conn.cursor() as cursor:
                # Update request - add ::jsonb cast for scraped_data
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET scrape_status = 'completed',
                        scraped_data = %s::jsonb,
                        scraped_at = NOW(),
                        merged_to_profile = true,
                        merged_at = NOW(),
                        place_id = %s,
                        updated_at = NOW()
                    WHERE lawyer_enrichment_id = %s AND source_key = %s
                    """,
                    (
                        psycopg2.extras.Json(scraped_data),
                        place_id,
                        profile['lawyer_enrichment_id'],
                        self.source_key,
                    ),
                )
                
                # Update lawyer_enrichment - add ::jsonb cast and set dedicated columns
                cursor.execute(
                    """
                    UPDATE lawyer_enrichment
                    SET merged_data = %s,
                        completeness_score = %s,
                        google_place_id = %s,
                        google_rating = %s,
                        google_review_count = %s,
                        geo_lat = %s,
                        geo_lng = %s,
                        raw_data_by_source = raw_data_by_source || jsonb_build_object(
                            %s,
                            jsonb_build_object(
                                'scraped_at', %s,
                                'layer', 4,
                                'place_id', %s,
                                'data', %s::jsonb
                            )
                        ),
                        enrichment_layers = CASE
                            WHEN NOT (enrichment_layers @> jsonb_build_array(%s))
                            THEN enrichment_layers || jsonb_build_array(%s)
                            ELSE enrichment_layers
                        END,
                        enrichment_level = GREATEST(enrichment_level, 4),
                        level_4_completed_at = NOW(),
                        last_enriched_at = NOW(),
                        ready_for_promotion = (
                            %s >= %s AND 
                            NOT promotion_blocked AND 
                            promoted_at IS NULL
                        ),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        psycopg2.extras.Json(merged_data),
                        completeness_score,
                        place_id,
                        google_rating,
                        google_review_count,
                        geo_lat,
                        geo_lng,
                        self.source_key,
                        datetime.now().isoformat(),
                        place_id,
                        psycopg2.extras.Json(scraped_data),
                        self.source_key,
                        self.source_key,
                        completeness_score,
                        completeness_threshold,
                        profile['lawyer_enrichment_id'],
                    ),
                )
                
                conn.commit()
        finally:
            conn.close()

    def _calculate_cost(self):
        """Calculate estimated API cost."""
        # Google Places API pricing (as of 2026):
        # Find Place from Text: $17 per 1,000 requests
        # Place Details: $17 per 1,000 requests
        
        find_place_cost = (self.stats['api_calls']['find_place'] / 1000) * 17
        details_cost = (self.stats['api_calls']['place_details'] / 1000) * 17
        
        self.stats['estimated_cost_usd'] = round(find_place_cost + details_cost, 2)


async def main():
    """Main entry point for standalone execution."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Google Places Worker (Layer 4)')
    parser.add_argument('--batch-size', type=int, default=500, help='Batch size')
    parser.add_argument('--min-completeness', type=int, default=60, help='Min completeness score')
    parser.add_argument('--refresh-days', type=int, default=30, help='Refresh interval in days')
    parser.add_argument('--skip-cost-check', action='store_true', help='Skip cost protection (DANGEROUS)')
    parser.add_argument('--dry-run', action='store_true', help='Dry run (no API calls)')
    
    args = parser.parse_args()
    
    if args.dry_run:
        logger.warning("Dry run mode not yet implemented")
        return
    
    # Warn if cost protection is disabled
    if args.skip_cost_check:
        logger.warning("=" * 60)
        logger.warning("⚠️  COST PROTECTION DISABLED")
        logger.warning("This batch will ignore daily limits and could incur large costs!")
        logger.warning("=" * 60)
        import time
        time.sleep(3)  # Give user time to cancel
    
    worker = GooglePlacesWorker(
        batch_size=args.batch_size,
        min_completeness=args.min_completeness,
        refresh_interval_days=args.refresh_days,
        skip_cost_check=args.skip_cost_check,
    )
    
    stats = await worker.process_batch()
    
    logger.info("=" * 60)
    logger.info("BATCH COMPLETE")
    logger.info("=" * 60)
    logger.info(f"Batch ID: {stats['batch_id']}")
    logger.info(f"Profiles selected: {stats['total_selected']}")
    logger.info(f"Discovery success: {stats['discovery_success']}")
    logger.info(f"Scrape success: {stats['scrape_success']}")
    logger.info(f"Skipped: {stats['skipped']}")
    logger.info(f"Errors: {stats['errors']}")
    logger.info(f"API calls - Find Place: {stats['api_calls']['find_place']}")
    logger.info(f"API calls - Place Details: {stats['api_calls']['place_details']}")
    logger.info(f"Estimated cost: ${stats['estimated_cost_usd']}")
    logger.info("=" * 60)


if __name__ == '__main__':
    asyncio.run(main())
