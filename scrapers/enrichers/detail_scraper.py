"""Detail page batch scraper for enriching level 1 lawyers."""

import asyncio
import os
from typing import List, Optional, Dict, Any
from datetime import datetime
import aiohttp
from bs4 import BeautifulSoup
import psycopg2
from psycopg2.extras import RealDictCursor
from dotenv import load_dotenv
from playwright.async_api import async_playwright, Browser, BrowserContext, Page
from scrapers.base import LawyerRawData
from scrapers.enrichers.base_detail import BaseDetailEnricher
from scrapers.enrichers.calbar_details import CaliforniaDetailEnricher
from scrapers.enrichers.ncbar_details import NorthCarolinaDetailEnricher
from scrapers.enrichers.nybar_details import NYBarDetailEnricher
from utils.rate_limiter import TokenBucketRateLimiter
from utils.logger import get_logger

logger = get_logger(__name__)


def _sanitize_detail_html(html: str) -> str:
    """Remove remote assets/scripts so set_content does not block on third-party resources."""
    soup = BeautifulSoup(html, 'html.parser')
    for tag in soup.find_all(['script', 'noscript', 'iframe', 'style', 'link']):
        tag.decompose()
    # Remove external image/media loads that could hang
    for img in soup.find_all('img'):
        img['src'] = ''
    return str(soup)


async def fetch_detail_html(detail_url: str, timeout_ms: int = 15000) -> str:
    """Fetch detail HTML directly instead of relying on browser navigation."""
    timeout = aiohttp.ClientTimeout(total=timeout_ms / 1000)
    headers = {
        'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    }
    async with aiohttp.ClientSession(timeout=timeout, headers=headers) as session:
        async with session.get(detail_url, ssl=False) as response:
            response.raise_for_status()
            return await response.text()


async def safe_page_goto(page: Page, detail_url: str, timeout_ms: int = 15000) -> None:
    """Navigate to a detail page without getting stuck on browser lifecycle waits."""
    try:
        raw_html = await fetch_detail_html(detail_url, timeout_ms=timeout_ms)
        sanitized_html = _sanitize_detail_html(raw_html)
        await page.set_content(sanitized_html, wait_until='commit', timeout=timeout_ms)
    except Exception as exc:
        logger.warning(
            "Detail page HTML fetch fallback triggered",
            detail_url=detail_url,
            timeout_ms=timeout_ms,
            error=str(exc),
        )
        # Final fallback: direct browser navigation.
        try:
            await page.goto(detail_url, wait_until='load', timeout=timeout_ms)
        except Exception as nav_exc:
            logger.warning(
                "Detail page browser navigation fallback triggered",
                detail_url=detail_url,
                error=str(nav_exc),
            )
            try:
                await asyncio.wait_for(page.wait_for_selector('body', timeout=5000), timeout=8)
            except Exception:
                await asyncio.wait_for(page.wait_for_selector('html', timeout=5000), timeout=8)

# Load environment variables
load_dotenv(override=True)


class DetailPageScraper:
    """
    Batch scraper for lawyer detail pages.

    Queries lawyers from database at a specific enrichment level,
    navigates to their detail pages, and extracts enriched data.
    """

    def __init__(
        self,
        state_code: str,
        enricher: BaseDetailEnricher,
        source_level: int = 1,
        target_level: int = 2,
        headless: bool = True,
        rate_limit: int = 30,  # requests per minute
        batch_tracker=None,
        limit: Optional[int] = None,
    ):
        """
        Initialize detail page scraper.

        Args:
            state_code: State code (e.g., 'CA', 'NY')
            enricher: Detail enricher instance for this state
            source_level: Enrichment level to query (default: 1)
            target_level: Target enrichment level after scraping (default: 2)
            headless: Run browser in headless mode
            rate_limit: Maximum requests per minute
            batch_tracker: Optional BatchTracker for progress updates
            limit: Maximum number of lawyers to process (None = all)
        """
        self.state_code = state_code
        self.enricher = enricher
        self.source_level = source_level
        self.target_level = target_level
        self.headless = headless
        self.batch_tracker = batch_tracker
        self.limit = limit

        # Rate limiting
        self.rate_limiter = TokenBucketRateLimiter(rate=rate_limit)

        # Statistics
        self.stats = {
            'total_lawyers': 0,
            'processed': 0,
            'succeeded': 0,
            'failed': 0,
            'not_found': 0,
            'skipped': 0,
            'start_time': None,
            'end_time': None,
        }

        # Browser objects
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        logger.info(
            "DetailPageScraper initialized",
            state=state_code,
            source_level=source_level,
            target_level=target_level,
            rate_limit=rate_limit,
        )

    def _get_db_connection(self):
        """Create database connection to scraper database."""
        return psycopg2.connect(
            host=os.getenv('SCRAPER_DB_HOST', '127.0.0.1'),
            port=int(os.getenv('SCRAPER_DB_PORT', 5433)),
            database=os.getenv('SCRAPER_DB_NAME', 'greenway_scraper'),
            user=os.getenv('SCRAPER_DB_USER', 'scraper'),
            password=os.getenv('SCRAPER_DB_PASSWORD', 'scraper_secret'),
        )

    def fetch_lawyers_for_enrichment(self) -> List[Dict[str, Any]]:
        """
        Query lawyers from database that need detail page enrichment.

        Returns:
            List of lawyer records with id, bar_number, full_name, etc.
        """
        conn = self._get_db_connection()
        try:
            with conn.cursor(cursor_factory=RealDictCursor) as cursor:
                query = """
                    SELECT
                        id,
                        fingerprint,
                        bar_number,
                        full_name,
                        license_state,
                        enrichment_level,
                        merged_data,
                        COALESCE(
                            merged_data->>'detail_url',
                            raw_data_by_source->'state_bars'->>'detail_url'
                        ) AS detail_url
                    FROM lawyer_enrichment
                    WHERE license_state = %s
                      AND enrichment_level = %s
                      AND bar_number IS NOT NULL
                    ORDER BY id
                """

                params = [self.state_code, self.source_level]

                if self.limit:
                    query += " LIMIT %s"
                    params.append(self.limit)

                cursor.execute(query, params)
                lawyers = cursor.fetchall()

                logger.info(
                    f"Fetched {len(lawyers)} lawyers for detail enrichment",
                    state=self.state_code,
                    level=self.source_level,
                )

                return [dict(row) for row in lawyers]

        finally:
            conn.close()

    async def scrape_detail_page(
        self,
        lawyer: Dict[str, Any],
    ) -> Optional[LawyerRawData]:
        """
        Scrape detail page for a single lawyer.

        Args:
            lawyer: Lawyer record from database

        Returns:
            Enriched LawyerRawData or None if failed
        """
        bar_number = lawyer['bar_number']
        full_name = lawyer['full_name']

        try:
            # Rate limiting
            await self.rate_limiter.acquire()

            # Use stored detail_url from listing if available, otherwise construct from bar_number
            detail_url = lawyer.get('detail_url') or self.enricher.construct_detail_url(bar_number)

            logger.debug(f"Navigating to detail page: {detail_url}")

            # Navigate to detail page
            if self.enricher.requires_browser_session:
                await self.enricher.navigate_to_detail_page(self.page, detail_url, bar_number)
            else:
                await safe_page_goto(self.page, detail_url, timeout_ms=15000)

            # Wait a bit for dynamic content
            await self.page.wait_for_timeout(1000)

            logger.info(
                "Detail page loaded",
                bar_number=bar_number,
                current_url=self.page.url,
                title=(await self.page.title()),
            )

            # Parse detail page
            # Create existing data object from database record
            existing_data = LawyerRawData(
                full_name=full_name,
                bar_number=bar_number,
                license_status=lawyer.get('merged_data', {}).get('license_status'),
            )

            enriched_data = await self.enricher.parse_detail_page(
                self.page,
                bar_number,
                existing_data
            )

            # Check if profile was not found
            if enriched_data is None:
                logger.warning(
                    f"Profile not found or no longer available",
                    bar_number=bar_number,
                    name=full_name,
                )
                self.stats['not_found'] += 1
                return None

            logger.info(
                f"Successfully scraped detail page",
                bar_number=bar_number,
                name=full_name,
            )

            self.stats['succeeded'] += 1
            return enriched_data

        except Exception as e:
            logger.error(
                f"Failed to scrape detail page",
                bar_number=bar_number,
                name=full_name,
                error=str(e),
            )
            self.stats['failed'] += 1
            return None

    async def run(self) -> List[LawyerRawData]:
        """
        Run the detail page scraping batch process.

        Returns:
            List of enriched LawyerRawData objects
        """
        self.stats['start_time'] = datetime.now()

        # Some enrichers are no-ops (e.g. NY Bar — data complete from CSV)
        if getattr(self.enricher, 'skip_level_2', False):
            logger.info(
                "Level 2 skipped — data already complete from Level 1 source",
                state=self.state_code,
            )
            return []

        # Fetch lawyers from database
        lawyers = self.fetch_lawyers_for_enrichment()
        self.stats['total_lawyers'] = len(lawyers)

        if not lawyers:
            logger.warning(
                "No lawyers found for detail enrichment",
                state=self.state_code,
                level=self.source_level,
            )
            return []

        enriched_lawyers = []

        async with async_playwright() as p:
            # Launch browser
            self.browser = await p.chromium.launch(
                headless=self.headless,
                args=['--disable-blink-features=AutomationControlled']
            )

            self.context = await self.browser.new_context(
                user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
            )

            self.page = await self.context.new_page()

            # Establish browser session if required (e.g. for ASP.NET session cookies)
            if self.enricher.requires_browser_session:
                await self.enricher.setup_browser_session(self.page)

            try:
                # Process each lawyer
                for idx, lawyer in enumerate(lawyers, start=1):
                    logger.info(
                        f"Processing lawyer {idx}/{len(lawyers)}",
                        bar_number=lawyer['bar_number'],
                        name=lawyer['full_name'],
                    )

                    # Scrape detail page
                    enriched_data = await self.scrape_detail_page(lawyer)

                    if enriched_data:
                        enriched_lawyers.append(enriched_data)

                    self.stats['processed'] += 1

                    # Update batch tracker
                    if self.batch_tracker:
                        progress_percent = (idx / len(lawyers)) * 100
                        self.batch_tracker.update_progress(
                            records_processed=idx,
                            records_created=0,  # Will be set by exporter
                            records_updated=self.stats['succeeded'],
                            records_failed=self.stats['failed'],
                            progress_percent=progress_percent,
                            metadata={
                                'current_lawyer': lawyer['full_name'],
                                'current_bar_number': lawyer['bar_number'],
                            }
                        )

                    # Small delay between requests (rate limiter handles this)
                    await asyncio.sleep(0.1)

            finally:
                # Cleanup
                await self.page.close()
                await self.context.close()
                await self.browser.close()

        self.stats['end_time'] = datetime.now()
        duration = (self.stats['end_time'] - self.stats['start_time']).total_seconds()

        logger.info(
            "Detail page scraping completed",
            state=self.state_code,
            total=self.stats['total_lawyers'],
            succeeded=self.stats['succeeded'],
            failed=self.stats['failed'],
            duration_seconds=duration,
        )

        return enriched_lawyers


def get_enricher_for_state(state_code: str) -> Optional[BaseDetailEnricher]:
    """
    Get the appropriate detail enricher for a state.

    Args:
        state_code: State code (e.g., 'CA', 'NY')

    Returns:
        Detail enricher instance or None if not implemented
    """
    if state_code == 'CA':
        return CaliforniaDetailEnricher()
    if state_code == 'NC':
        return NorthCarolinaDetailEnricher()
    if state_code == 'NY':
        return NYBarDetailEnricher()

    logger.error(f"No detail enricher implemented for state: {state_code}")
    return None
