"""California State Bar scraper.

CA Bar website: https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch

Note: California Bar Association website uses JavaScript/AJAX (DataTables) to load results.
The scraper must interact with the search form and wait for dynamic content.
Pagination is handled via DataTables buttons (not URL-based).
"""

from typing import AsyncIterator
from scrapers.base import BaseScraper, LawyerRawData, PaginationInfo
from playwright.async_api import Page
from bs4 import BeautifulSoup
from utils.logger import get_logger

logger = get_logger(__name__)


class StateBarCalifornia(BaseScraper):
    """
    Scraper for California State Bar member directory.

    CA Bar website uses DataTables with AJAX to load results.
    We need to submit the search form and wait for results to populate,
    then navigate through pages using DataTables pagination buttons.
    """

    BASE_URL = "https://apps.calbar.ca.gov"
    STATE_CODE = "california"
    RATE_LIMIT = 20  # requests per minute
    SOURCE = "calbar"

    # CA-specific search URL (this is the form page, not results)
    SEARCH_URL = "https://apps.calbar.ca.gov/attorney/LicenseeSearch/QuickSearch"

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._search_performed = False

    async def _perform_search(self, page: Page, search_term: str = "a"):
        """
        Perform initial search by filling form and submitting.

        Args:
            page: Playwright page
            search_term: Search term to use (default "a" for alphabet sweep)
        """
        logger.info("Performing initial search", search_term=search_term)

        # Wait for search input to be available
        await page.wait_for_selector('#FreeText', timeout=10000)

        # Fill in search term
        await page.fill('#FreeText', search_term)

        # Click search button
        await page.click('button[type="submit"]')

        # Wait for results to load - Kendo grid typically has specific classes
        # Try waiting for network idle and a reasonable delay for AJAX
        await page.wait_for_load_state('networkidle', timeout=30000)

        # Give it extra time for AJAX to complete
        await page.wait_for_timeout(2000)

        self._search_performed = True
        logger.info("Search form submitted, waiting for results")

    async def parse_listing(self, page: Page) -> AsyncIterator[LawyerRawData]:
        """
        Parse lawyer listings from CA Bar search results.

        The CA Bar website uses Kendo UI grid which loads via AJAX.
        Results appear in a div or table after form submission.

        Args:
            page: Playwright page with loaded content

        Yields:
            LawyerRawData objects
        """
        # If we haven't performed the search yet, do it now
        if not self._search_performed:
            await self._perform_search(page)

        # Get the updated page content after AJAX load
        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')

        # Look for Kendo grid or results table
        # Common selectors: .k-grid, #grid, or look for tables with specific classes
        results_container = soup.find('div', class_=lambda x: x and 'k-grid' in x) if soup.find('div', class_=lambda x: x and 'k-grid' in x) else soup.find('table', class_='dataTable')

        if not results_container:
            # Try to find any table after the search form
            results_container = soup.find('table')

        if not results_container:
            logger.warning("No results container found on page")
            # Save HTML for debugging
            logger.debug("Page HTML length", length=len(html))
            return

        # Parse table rows
        rows = results_container.find_all('tr')

        if len(rows) <= 1:
            logger.warning("No result rows found", total_rows=len(rows))
            return

        # Skip header row
        data_rows = rows[1:]
        logger.info("Found result rows", count=len(data_rows))

        for row in data_rows:
            try:
                cols = row.find_all('td')
                if len(cols) < 3:  # Need at least name, status, bar number
                    continue

                # Parse columns - structure: Name, Status, Bar Number, City, Admission Date
                # Col 0: Name + Detail link
                name_col = cols[0]
                name_link = name_col.find('a')
                detail_url = None
                if name_link:
                    detail_url = name_link.get('href', '')
                    if detail_url and not detail_url.startswith('http'):
                        detail_url = f"{self.BASE_URL}{detail_url}"

                full_name = name_col.get_text(strip=True)

                if not full_name:  # Skip empty rows
                    continue

                # Parse other columns
                license_status = cols[1].get_text(strip=True) if len(cols) > 1 else None
                bar_number = cols[2].get_text(strip=True) if len(cols) > 2 else None
                city = cols[3].get_text(strip=True) if len(cols) > 3 else None

                yield LawyerRawData(
                    full_name=full_name,
                    bar_number=bar_number,
                    license_status=license_status,
                    address=None,       # list page only has city; full address comes from Level 2
                    detail_url=detail_url,
                    raw_html=str(row),
                )

            except Exception as e:
                logger.error("Failed to parse lawyer row", error=str(e))
                continue

    async def get_pagination_info(self, page: Page) -> PaginationInfo:
        """
        Extract pagination information from CA Bar results page.

        CA Bar uses DataTables (not Kendo) for pagination controls.

        Args:
            page: Playwright page with loaded content

        Returns:
            PaginationInfo object
        """
        current_page = 1
        total_pages = None
        has_next = False

        try:
            # DataTables info text: "Showing 1 to 50 of 500 entries"
            info_elem = await page.query_selector('.dataTables_info')
            if info_elem:
                info_text = await info_elem.text_content()
                logger.info(f"DataTables info: {info_text}")

                # Parse total entries if available
                import re
                match = re.search(r'of (\d+) entries', info_text)
                if match:
                    total_entries = int(match.group(1))
                    # Assuming 50 per page
                    total_pages = (total_entries + 49) // 50
                    logger.info(f"Calculated total pages: {total_pages} from {total_entries} entries")

            # Check for DataTables Next button (not disabled)
            next_button = await page.query_selector('a.paginate_button.next:not(.disabled)')
            has_next = next_button is not None
            logger.info(f"DataTables Next button found: {has_next}")

            # Try to get current page from the "current" button
            current_elem = await page.query_selector('a.paginate_button.current')
            if current_elem:
                page_text = await current_elem.text_content()
                try:
                    current_page = int(page_text.strip())
                    logger.info(f"Current page: {current_page}")
                except:
                    pass

        except Exception as e:
            logger.error("Error detecting pagination", error=str(e))
            has_next = False

        logger.info("Pagination info", current_page=current_page, total_pages=total_pages, has_next=has_next)

        return PaginationInfo(
            current_page=current_page,
            total_pages=total_pages,
            has_next=has_next,
            next_url=None,  # We'll navigate via button click
        )

    def _get_page_url(self, page: int) -> str:
        """
        Get the URL for page navigation.

        For CA Bar, we start at the search form URL and use JavaScript
        pagination after initial search.

        Args:
            page: Page number

        Returns:
            Search form URL (pagination happens via JavaScript)
        """
        # Always return base search URL - pagination is handled via JavaScript
        return self.SEARCH_URL

    async def navigate_to_next_page(self) -> bool:
        """
        Navigate to the next page by clicking the Next button in DataTables.

        Returns:
            True if navigation was successful, False otherwise
        """
        try:
            # Find the DataTables Next button (not disabled)
            next_button = await self.page.query_selector('a.paginate_button.next:not(.disabled)')

            if not next_button:
                logger.warning("Next button not found or is disabled")
                return False

            logger.info("Clicking DataTables Next button")

            # Click the button
            await next_button.click()

            # Wait for the AJAX request to complete
            # DataTables typically updates via AJAX
            await self.page.wait_for_load_state('networkidle', timeout=30000)

            # Give extra time for table to update
            await self.page.wait_for_timeout(2000)

            logger.info("Successfully navigated to next page")
            return True

        except Exception as e:
            logger.error("Failed to navigate to next page", error=str(e))
            return False

    async def run(self) -> list[LawyerRawData]:
        """
        Run the scraper with custom pagination handling for DataTables.

        Uses alphabet sweep strategy: searches for a-z to capture all lawyers.
        For each search term, paginates through all result pages.

        Returns:
            List of all LawyerRawData objects collected
        """
        import time
        import string
        from datetime import datetime
        from playwright.async_api import async_playwright

        self.stats['start_time'] = datetime.now().isoformat()
        all_lawyers = []

        logger.info("Starting scrape for california...")

        async with async_playwright() as playwright:
            # Launch browser
            self.browser = await playwright.chromium.launch(headless=self.headless)
            self.context = await self.browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            self.page = await self.context.new_page()

            try:
                # Alphabet sweep: search for each letter a-z to capture all lawyers
                search_terms = list(string.ascii_lowercase)

                # Check for checkpoint to resume from the correct letter
                resume_from_letter = None
                if self.checkpoint_enabled and self.checkpoint:
                    checkpoint_data = self.checkpoint.load()
                    if checkpoint_data and 'stats' in checkpoint_data:
                        resume_from_letter = checkpoint_data['stats'].get('current_search_term')
                        if resume_from_letter:
                            logger.info(
                                f"📍 Checkpoint found - resuming from letter '{resume_from_letter}'",
                                checkpoint_page=checkpoint_data.get('last_page'),
                                lawyers_found=checkpoint_data['stats'].get('lawyers_found', 0)
                            )
                        else:
                            logger.info("No previous search term in checkpoint, starting from 'a'")
                    else:
                        logger.info("No checkpoint data found, starting from 'a'")

                logger.info(f"Starting alphabet sweep with {len(search_terms)} search terms: {', '.join(search_terms)}")

                for search_term in search_terms:
                    # Skip letters before the resume point
                    if resume_from_letter and search_term < resume_from_letter:
                        logger.info(f"⏭️  Skipping letter '{search_term}' (already completed)")
                        continue

                    logger.info(f"=== Starting alphabet sweep for letter: {search_term} ===")

                    # Reset search state
                    self._search_performed = False
                    current_page = 1

                    # Navigate to search page
                    start_url = self._get_page_url(current_page)
                    await self.fetch_page(start_url)

                    # Perform search for this letter
                    await self._perform_search(self.page, search_term=search_term)

                    # Process all pages for this search term
                    while True:
                        logger.info(
                            "Processing page",
                            page=current_page,
                            search_term=search_term,
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
                                search_term=search_term,
                                lawyers_found=len(page_lawyers),
                                total_found=self.stats['lawyers_found'],
                            )

                            # Update batch progress in database
                            if self.batch_tracker:
                                self.batch_tracker.update_progress(
                                    records_processed=self.stats['lawyers_found'],
                                    records_failed=self.stats['errors'],
                                    metadata={'current_letter': search_term, 'pages_processed': self.stats['pages_processed']}
                                )

                        except Exception as e:
                            self.stats['errors'] += 1
                            logger.error(
                                "Failed to parse page",
                                page=current_page,
                                search_term=search_term,
                                error=str(e),
                            )
                            # Try to continue to next page

                        # Save checkpoint (using cumulative page count)
                        if self.checkpoint_enabled:
                            checkpoint_data = self.stats.copy()
                            checkpoint_data['current_search_term'] = search_term
                            self.checkpoint.save(self.stats['pages_processed'], checkpoint_data)

                        # Check pagination for this search term
                        try:
                            pagination = await self.get_pagination_info(self.page)

                            if not pagination.has_next:
                                logger.info(f"No more pages for '{search_term}', moving to next letter", final_page=current_page)
                                break

                            # Navigate to next page
                            current_page += 1

                            # Apply rate limiting before navigation
                            await self.rate_limiter.acquire()

                            success = await self.navigate_to_next_page()
                            if not success:
                                logger.warning(f"Failed to navigate to next page for '{search_term}', moving to next letter")
                                break

                        except Exception as e:
                            logger.error(
                                "Failed to get pagination",
                                page=current_page,
                                search_term=search_term,
                                error=str(e),
                            )
                            break

                    logger.info(f"=== Completed alphabet sweep for letter: {search_term} === (Total so far: {self.stats['lawyers_found']})")

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
