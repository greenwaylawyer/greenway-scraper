"""
Discovery Worker for Layer 3 & 4 enrichment.

Polls enrichment_source_requests where discovery_status='pending' and runs
the appropriate discovery strategies to find profile URLs on third-party sources.
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

from config.loader import get_source_config, get_confidence_threshold, get_enabled_sources
from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
from utils.logger import get_logger

logger = get_logger(__name__)
load_dotenv(override=True)


class DiscoveryWorker:
    """
    Worker that processes discovery requests for enrichment sources.
    
    1. Polls enrichment_source_requests WHERE discovery_status='pending'
    2. Loads appropriate enricher class for the source
    3. Runs discovery (search for profile on source)
    4. Scores candidates
    5. Auto-selects if confidence >= threshold
    6. Updates discovery_status, discovery_candidates, source_profile_url
    7. Sets scrape_status='queued' if profile found
    """

    def __init__(
        self,
        source_key: Optional[str] = None,
        batch_size: int = 20,
        max_attempts: int = 3,
    ):
        """
        Initialize discovery worker.
        
        Args:
            source_key: Process only this source (None = all enabled sources)
            batch_size: Number of requests to process per poll cycle
            max_attempts: Maximum discovery attempts before marking not_found
        """
        self.source_key = source_key
        self.batch_size = batch_size
        self.max_attempts = max_attempts
        self.enrichers = {}  # Cache enricher instances
        
        # Stats
        self.stats = {
            'total_processed': 0,
            'found': 0,
            'candidates': 0,
            'not_found': 0,
            'skipped': 0,
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

    def get_enricher(self, source_key: str) -> Optional[BaseEnricher]:
        """
        Get or create enricher instance for a source.
        
        Args:
            source_key: Source identifier
        
        Returns:
            Enricher instance or None if not available
        """
        if source_key in self.enrichers:
            return self.enrichers[source_key]
        
        config = get_source_config(source_key)
        if not config:
            logger.error(f"No config found for source: {source_key}")
            return None
        
        # Dynamic import of enricher class
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

    def fetch_pending_requests(self, limit: int) -> List[Dict[str, Any]]:
        """
        Fetch pending discovery requests from database.
        
        Args:
            limit: Maximum number of requests to fetch
        
        Returns:
            List of request dicts with lawyer data
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
                        esr.discovery_attempts,
                        esr.priority,
                        le.full_name,
                        le.first_name,
                        le.last_name,
                        le.bar_number,
                        le.license_state AS state,
                        le.city,
                        le.firm_name,
                        le.merged_data
                    FROM enrichment_source_requests esr
                    JOIN lawyer_enrichment le ON le.id = esr.lawyer_enrichment_id
                    WHERE esr.discovery_status = 'pending'
                      AND esr.discovery_attempts < %s
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
        Process a single discovery request.
        
        Args:
            request: Request dict from database
        
        Returns:
            True if successful, False if error
        """
        request_id = request['request_id']
        source_key = request['source_key']
        
        logger.info(
            f"Processing discovery request {request_id}",
            source=source_key,
            lawyer=request['full_name'],
            bar_number=request.get('bar_number'),
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
            
            # Run discovery
            merged_data = request.get('merged_data') or {}
            addr = merged_data.get('address') or {}
            address_str = ''
            if isinstance(addr, dict):
                parts = [addr.get('line1'), addr.get('line2'), addr.get('city'), addr.get('state'), addr.get('zip')]
                address_str = ', '.join([p for p in parts if p])

            lawyer_data = {
                'full_name': request['full_name'],
                'first_name': request['first_name'],
                'last_name': request['last_name'],
                'bar_number': request.get('bar_number'),
                'state': request.get('state'),
                'license_state': request.get('state'),
                'city': request.get('city'),
                'firm_name': request.get('firm_name'),
                'phone': merged_data.get('phone'),
                'address': address_str,
            }
            
            candidates = await enricher.discover_profile(lawyer_data)
            
            # Get confidence threshold for this source
            threshold = get_confidence_threshold(source_key)
            
            if not candidates:
                # No candidates found
                await self._update_request_not_found(request_id)
                self.stats['not_found'] += 1
                logger.info(f"No candidates found for request {request_id}")
                return True
            
            # Sort by confidence
            candidates.sort(key=lambda c: c.confidence, reverse=True)
            best = candidates[0]
            
            # Auto-select if confidence >= threshold
            if best.confidence >= threshold:
                await self._update_request_found(
                    request_id,
                    best.url,
                    [c.to_dict() for c in candidates],
                    best.match_signals[0] if best.match_signals else 'high_confidence',
                    best.confidence,
                )
                self.stats['found'] += 1
                logger.info(
                    f"Auto-selected profile for request {request_id}",
                    url=best.url,
                    confidence=best.confidence,
                )
                return True

            # Google Maps is often a single clear candidate even with low confidence
            # (street address/phone may be missing in our base profile). If there's only
            # one candidate, auto-select it and proceed to Layer 4 scraping.
            if source_key == 'google_maps' and len(candidates) == 1 and best.url:
                await self._update_request_found(
                    request_id,
                    best.url,
                    [c.to_dict() for c in candidates],
                    best.match_signals[0] if best.match_signals else 'single_candidate',
                    best.confidence,
                )
                self.stats['found'] += 1
                logger.info(
                    f"Auto-selected single Google Maps candidate for request {request_id}",
                    url=best.url,
                    confidence=best.confidence,
                )
                return True
            
            # Multiple candidates or low confidence — admin review needed
            await self._update_request_candidates(
                request_id,
                [c.to_dict() for c in candidates],
            )
            self.stats['candidates'] += 1
            logger.info(
                f"Multiple candidates for request {request_id}, admin review required",
                count=len(candidates),
                best_confidence=best.confidence,
            )
            return True
            
        except Exception as e:
            logger.error(
                f"Discovery failed for request {request_id}: {e}",
                exc_info=True,
            )
            await self._update_request_error(request_id, str(e))
            self.stats['errors'] += 1
            return False

    async def _update_request_found(
        self,
        request_id: int,
        profile_url: str,
        candidates: List[Dict],
        method: str,
        confidence: float,
    ):
        """Update request with found profile URL."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET discovery_status = 'found',
                        source_profile_url = %s,
                        discovery_method = %s,
                        discovery_candidates = %s,
                        match_confidence = %s,
                        discovery_completed_at = NOW(),
                        scrape_status = 'queued',
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (
                        profile_url,
                        method,
                        psycopg2.extras.Json(candidates),
                        confidence,
                        request_id,
                    ),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_candidates(
        self,
        request_id: int,
        candidates: List[Dict],
    ):
        """Update request with candidate list for admin review."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET discovery_status = 'candidates',
                        discovery_candidates = %s,
                        discovery_completed_at = NOW(),
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (psycopg2.extras.Json(candidates), request_id),
                )
                conn.commit()
        finally:
            conn.close()

    async def _update_request_not_found(self, request_id: int):
        """Update request as not found."""
        conn = self._get_db_connection()
        try:
            with conn.cursor() as cursor:
                cursor.execute(
                    """
                    UPDATE enrichment_source_requests
                    SET discovery_status = 'not_found',
                        discovery_completed_at = NOW(),
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
                    SET discovery_attempts = discovery_attempts + 1,
                        scrape_error = %s,
                        discovery_status = CASE 
                            WHEN discovery_attempts + 1 >= %s THEN 'not_found'
                            ELSE discovery_status
                        END,
                        discovery_completed_at = CASE 
                            WHEN discovery_attempts + 1 >= %s THEN NOW()
                            ELSE discovery_completed_at
                        END,
                        updated_at = NOW()
                    WHERE id = %s
                    """,
                    (error, self.max_attempts, self.max_attempts, request_id),
                )
                conn.commit()
        finally:
            conn.close()

    async def run_once(self):
        """Run one poll cycle."""
        requests = self.fetch_pending_requests(self.batch_size)
        
        if not requests:
            logger.debug("No pending discovery requests")
            return
        
        logger.info(f"Processing {len(requests)} discovery requests")
        
        for request in requests:
            await self.process_request(request)
            self.stats['total_processed'] += 1
            
            # Small delay between requests
            await asyncio.sleep(0.5)
        
        logger.info(
            "Discovery cycle complete",
            **self.stats,
        )

    async def run(self, poll_interval: int = 30):
        """
        Run worker in continuous poll mode.
        
        Args:
            poll_interval: Seconds between poll cycles
        """
        logger.info(
            "Discovery worker started",
            source_key=self.source_key or "all",
            batch_size=self.batch_size,
            poll_interval=poll_interval,
        )
        
        while True:
            try:
                await self.run_once()
            except Exception as e:
                logger.error(f"Discovery worker error: {e}", exc_info=True)
            
            await asyncio.sleep(poll_interval)


async def main():
    """Main entry point for standalone execution."""
    import argparse
    
    parser = argparse.ArgumentParser(description='Discovery Worker')
    parser.add_argument('--source', help='Process only this source')
    parser.add_argument('--batch-size', type=int, default=20, help='Batch size')
    parser.add_argument('--once', action='store_true', help='Run once and exit')
    parser.add_argument('--poll-interval', type=int, default=30, help='Poll interval in seconds')
    
    args = parser.parse_args()
    
    worker = DiscoveryWorker(
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
