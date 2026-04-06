"""Base detail page enricher abstract class for all state bar detail scrapers."""

from abc import ABC, abstractmethod
from typing import Optional, Dict, List
from playwright.async_api import Page
from scrapers.base import LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


class BaseDetailEnricher(ABC):
    """
    Abstract base class for detail page enrichers.

    Provides common functionality for scraping lawyer detail pages
    to enrich existing basic data with additional information like
    contact details, practice areas, education, etc.
    """

    # Subclasses should define these constants
    BASE_URL: str
    STATE_CODE: str
    SOURCE: str  # e.g., 'calbar_details', 'nybar_details'

    def __init__(self):
        """Initialize the detail enricher."""
        logger.info(
            "Detail enricher initialized",
            state=self.STATE_CODE,
            source=self.SOURCE,
        )

    @property
    def requires_browser_session(self) -> bool:
        """Return True if this site requires an established browser session before accessing detail pages."""
        return False

    async def navigate_to_detail_page(self, page, detail_url: str, bar_number: str) -> None:
        """
        Navigate to a detail page. Override for sites that require a specific navigation
        flow (e.g. going through search results before viewer pages are accessible).
        Default: direct page.goto().
        """
        await page.goto(detail_url, wait_until='domcontentloaded', timeout=15000)

    async def setup_browser_session(self, page) -> None:
        """
        Called once before detail page scraping begins.
        Override to establish any required session state (e.g. visit a search form).
        """
        pass

    @abstractmethod
    async def parse_detail_page(
        self,
        page: Page,
        bar_number: str,
        existing_data: Optional[LawyerRawData] = None
    ) -> Optional[LawyerRawData]:
        """
        Parse a lawyer detail page and extract enriched data.

        Args:
            page: Playwright page object with loaded detail page
            bar_number: Bar number for this lawyer
            existing_data: Optional existing data to merge with

        Returns:
            LawyerRawData with enriched information, or None if profile not found
        """
        pass

    @abstractmethod
    async def expand_additional_info(self, page: Page) -> bool:
        """
        Expand "More About" or additional information sections if present.

        Args:
            page: Playwright page object

        Returns:
            True if section was found and expanded, False otherwise
        """
        pass

    async def extract_contact_info(self, page: Page) -> Dict[str, Optional[str]]:
        """
        Extract contact information from detail page.

        Args:
            page: Playwright page object

        Returns:
            Dictionary with keys: address, phone, fax, email, website_url
        """
        return {
            'address': await self._extract_address(page),
            'phone': await self._extract_phone(page),
            'fax': await self._extract_fax(page),
            'email': await self._extract_email(page),
            'website_url': await self._extract_website(page),
        }

    @abstractmethod
    async def _extract_address(self, page: Page) -> Optional[str]:
        """Extract full address from detail page."""
        pass

    @abstractmethod
    async def _extract_phone(self, page: Page) -> Optional[str]:
        """Extract phone number from detail page."""
        pass

    @abstractmethod
    async def _extract_fax(self, page: Page) -> Optional[str]:
        """Extract fax number from detail page."""
        pass

    @abstractmethod
    async def _extract_email(self, page: Page) -> Optional[str]:
        """Extract email address from detail page."""
        pass

    @abstractmethod
    async def _extract_website(self, page: Page) -> Optional[str]:
        """Extract website URL from detail page."""
        pass

    async def extract_practice_areas(self, page: Page) -> List[str]:
        """
        Extract practice areas from detail page.

        Args:
            page: Playwright page object

        Returns:
            List of practice area names
        """
        return []

    async def extract_education(self, page: Page) -> Dict[str, Optional[str]]:
        """
        Extract education information from detail page.

        Args:
            page: Playwright page object

        Returns:
            Dictionary with law school and admission year
        """
        return {
            'law_school': await self._extract_law_school(page),
            'admission_year': await self._extract_admission_year(page),
        }

    @abstractmethod
    async def _extract_law_school(self, page: Page) -> Optional[str]:
        """Extract law school from detail page."""
        pass

    async def _extract_admission_year(self, page: Page) -> Optional[int]:
        """Extract admission/bar year from detail page."""
        return None

    async def extract_languages(self, page: Page) -> List[str]:
        """
        Extract spoken languages from detail page.

        Args:
            page: Playwright page object

        Returns:
            List of language names
        """
        return []

    async def extract_firm_name(self, page: Page) -> Optional[str]:
        """
        Extract firm name from detail page.

        Args:
            page: Playwright page object

        Returns:
            Firm name if available
        """
        return None

    async def normalize_name(self, page: Page) -> Dict[str, Optional[str]]:
        """
        Extract and normalize the lawyer's name from detail page.

        This is useful for correcting list-scraped names which may be
        formatted differently (e.g., "LAST, FIRST" vs "First Last").

        Args:
            page: Playwright page object

        Returns:
            Dictionary with full_name, first_name, last_name
        """
        return {
            'full_name': None,
            'first_name': None,
            'last_name': None,
        }

    def construct_detail_url(self, bar_number: str) -> str:
        """
        Construct the detail page URL from a bar number.

        Args:
            bar_number: Bar/license number

        Returns:
            Full URL to detail page
        """
        # Default implementation - subclasses should override if needed
        return f"{self.BASE_URL}/attorney/Licensee/Detail/{bar_number}"

    async def wait_for_page_load(self, page: Page, timeout: int = 10000) -> None:
        """
        Wait for detail page to fully load.

        Args:
            page: Playwright page object
            timeout: Maximum wait time in milliseconds
        """
        # Some directories keep analytics/network requests alive, so
        # waiting for networkidle can hang. Prefer DOM readiness and a
        # short settle delay for stable parsing.
        try:
            await page.wait_for_load_state('domcontentloaded', timeout=timeout)
            await page.wait_for_timeout(600)
        except Exception as e:
            logger.warning(
                "Page load timeout",
                error=str(e),
                state=self.STATE_CODE,
            )
