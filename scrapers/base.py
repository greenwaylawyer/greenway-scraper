"""Base scraper abstract class for all state bar scrapers."""

from abc import ABC, abstractmethod
from typing import AsyncIterator, Optional
from dataclasses import dataclass
import asyncio
from playwright.async_api import async_playwright, Page, Browser, BrowserContext
from utils.rate_limiter import TokenBucketRateLimiter
from utils.checkpoint import CheckpointManager
from utils.logger import get_logger

logger = get_logger(__name__)


@dataclass
class PaginationInfo:
    """Pagination information from a listing page."""
    current_page: int
    total_pages: Optional[int] = None
    total_results: Optional[int] = None
    has_next: bool = False
    next_url: Optional[str] = None


@dataclass
class LawyerRawData:
    """Raw lawyer data extracted from a listing page."""
    full_name: str
    middle_name: Optional[str] = None
    bar_number: Optional[str] = None
    firm_name: Optional[str] = None
    address: Optional[str] = None
    phone: Optional[str] = None
    fax: Optional[str] = None
    email: Optional[str] = None
    website_url: Optional[str] = None
    practice_areas: Optional[list[str]] = None
    bio: Optional[str] = None
    photo_url: Optional[str] = None
    law_school: Optional[str] = None
    admission_year: Optional[int] = None
    license_status: Optional[str] = None
    detail_url: Optional[str] = None
    raw_html: Optional[str] = None


class BaseScraper(ABC):
    """
    Abstract base class for state bar scrapers.

    Provides common functionality:
    - Rate limiting with token bucket algorithm
    - Checkpoint-based resume capability
    - Headless browser automation with Playwright
    - Progress tracking and error handling
    """

    # Subclasses should define these constants
    BASE_URL: str
    STATE_CODE: str
    RATE_LIMIT: int = 20  # requests per minute
    SOURCE: str  # e.g., 'calbar', 'nybar'

    def __init__(
        self,
        start_page: int = 1,
        headless: bool = True,
        checkpoint_enabled: bool = True,
        batch_tracker = None,
        limit: Optional[int] = None
    ):
        """
        Initialize the scraper.

        Args:
            start_page: Page number to start from (for resume capability)
            headless: Whether to run browser in headless mode
            checkpoint_enabled: Whether to enable checkpoint saving
            batch_tracker: Optional BatchTracker instance for live status updates
            limit: Maximum number of records to scrape (None = no limit, useful for testing)
        """
        self.start_page = start_page
        self.headless = headless
        self.checkpoint_enabled = checkpoint_enabled
        self.batch_tracker = batch_tracker
        self.limit = limit

        # Initialize components
        self.rate_limiter = TokenBucketRateLimiter(rate=self.RATE_LIMIT)
        self.checkpoint = CheckpointManager(self.STATE_CODE) if checkpoint_enabled else None

        # Statistics
        self.stats = {
            'pages_processed': 0,
            'lawyers_found': 0,
            'errors': 0,
            'start_time': None,
            'end_time': None,
        }

        # Playwright objects (set in run())
        self.browser: Optional[Browser] = None
        self.context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

        logger.info(
            "Scraper initialized",
            state=self.STATE_CODE,
            source=self.SOURCE,
            start_page=start_page,
            rate_limit=self.RATE_LIMIT,
        )

    @abstractmethod
    async def parse_listing(self, page: Page) -> AsyncIterator[LawyerRawData]:
        """
        Parse lawyer listings from a search results page.

        Args:
            page: Playwright page object with loaded content

        Yields:
            LawyerRawData objects for each lawyer found

        This method must be implemented by each state scraper
        to handle the specific HTML structure of that state's bar website.
        """
        pass

    @abstractmethod
    async def get_pagination_info(self, page: Page) -> PaginationInfo:
        """
        Extract pagination information from current page.

        Args:
            page: Playwright page object with loaded content

        Returns:
            PaginationInfo object with current page, total pages, etc.

        This method must be implemented by each state scraper
        to handle their specific pagination structure.
        """
        pass

    async def fetch_page(self, url: str) -> Page:
        """
        Fetch a page with rate limiting.

        Args:
            url: URL to fetch

        Returns:
            Loaded Playwright page object
        """
        # Acquire rate limit token
        await self.rate_limiter.acquire()

        # Navigate to URL
        logger.debug("Fetching page", url=url)
        await self.page.goto(url, wait_until="networkidle", timeout=30000)

        return self.page

    async def run(self) -> list[LawyerRawData]:
        """
        Run the scraper from start_page.

        Returns:
            List of all LawyerRawData objects collected
        """
        import time
        from datetime import datetime

        self.stats['start_time'] = datetime.now().isoformat()
        all_lawyers = []

        logger.info(
            "Starting scrape",
            state=self.STATE_CODE,
            start_page=self.start_page,
        )

        async with async_playwright() as playwright:
            # Launch browser
            self.browser = await playwright.chromium.launch(headless=self.headless)
            self.context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            self.page = await self.context.new_page()

            try:
                # Start from the specified page
                current_page = self.start_page
                start_url = self._get_page_url(current_page)

                await self.fetch_page(start_url)

                # Process pages
                while True:
                    logger.info(
                        "Processing page",
                        page=current_page,
                        url=self.page.url,
                    )

                    # Parse listings from this page
                    try:
                        page_lawyers = []
                        async for lawyer in self.parse_listing(self.page):
                            page_lawyers.append(lawyer)
                            all_lawyers.append(lawyer)

                        self.stats['lawyers_found'] += len(page_lawyers)
                        self.stats['pages_processed'] += 1

                        logger.info(
                            "Page processed",
                            page=current_page,
                            lawyers_found=len(page_lawyers),
                            total_found=self.stats['lawyers_found'],
                        )

                    except Exception as e:
                        self.stats['errors'] += 1
                        logger.error(
                            "Failed to parse page",
                            page=current_page,
                            error=str(e),
                        )
                        # Try to continue to next page

                    # Save checkpoint
                    if self.checkpoint_enabled:
                        self.checkpoint.save(current_page, self.stats)

                    # Check pagination
                    try:
                        pagination = await self.get_pagination_info(self.page)

                        if not pagination.has_next:
                            logger.info("No more pages", final_page=current_page)
                            break

                        # Go to next page
                        current_page += 1
                        if pagination.next_url:
                            next_url = pagination.next_url
                        else:
                            next_url = self._get_page_url(current_page)

                        await self.fetch_page(next_url)

                    except Exception as e:
                        logger.error(
                            "Failed to get pagination",
                            page=current_page,
                            error=str(e),
                        )
                        break

            finally:
                # Cleanup
                await self.context.close()
                await self.browser.close()

        self.stats['end_time'] = datetime.now().isoformat()

        logger.info(
            "Scrape complete",
            state=self.STATE_CODE,
            total_lawyers=len(all_lawyers),
            stats=self.stats,
        )

        return all_lawyers

    @abstractmethod
    def _get_page_url(self, page: int) -> str:
        """
        Get the URL for a specific page number.

        Args:
            page: Page number

        Returns:
            Full URL for that page

        Must be implemented by each state scraper
        to handle their URL structure.
        """
        pass
