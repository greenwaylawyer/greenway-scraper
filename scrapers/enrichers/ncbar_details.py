"""North Carolina State Bar detail page enricher.

NC Bar detail page URL:
    https://portal.ncbar.gov/Verification/viewer.aspx?ID=<INTERNAL_ID>

Page structure (Bootstrap definition list):
    <div class="panel-body">
        <dl class="dl-horizontal">
            <dt>Bar #:</dt>
            <dd>50546</dd>
            <dt>Name:</dt>
            <dd>Ms. Morgan Parker Abbott</dd>
            <dt>Address:</dt>
            <dd>310 Blackwell Street, 4th Fl.<br />Box 104124</dd>
            <dt>City:</dt>
            <dd>Durham</dd>
            <dt>State:</dt>
            <dd>NC</dd>
            <dt>Zip Code:</dt>
            <dd>27710</dd>
            <dt>Work Phone:</dt>
            <dd>919-681-5457</dd>
            <dt>Email:</dt>
            <dd>morgan.abbott@duke.edu</dd>
            <dt>Status:</dt>
            <dd><span class="label label-success">Active</span></dd>
            <dt>Date Admitted:</dt>
            <dd>08/26/2016</dd>
            <dt>Judicial District:</dt>
            <dd>18 - Chatham, Orange</dd>
        </dl>
    </div>

The panel heading contains: "Ms. Morgan Parker Abbott - Attorney"
"""

import re
from typing import Optional

from bs4 import BeautifulSoup
from playwright.async_api import Page

from scrapers.enrichers.base_detail import BaseDetailEnricher
from scrapers.base import LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


class NorthCarolinaDetailEnricher(BaseDetailEnricher):
    """
    Enricher for NC Bar member detail pages.

    Scrapes https://portal.ncbar.gov/Verification/viewer.aspx?ID=<ID>
    and returns a populated LawyerRawData.
    """

    BASE_URL = "https://portal.ncbar.gov"
    STATE_CODE = "NC"
    SOURCE = "ncbar_details"

    @property
    def requires_browser_session(self) -> bool:
        return True

    async def setup_browser_session(self, page) -> None:
        """Visit the NC Bar search form once to initialise the ASP.NET session cookie."""
        logger.info("NC Bar: initialising browser session")
        await page.goto(
            "https://portal.ncbar.gov/Verification/search.aspx",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(1000)
        logger.info("NC Bar: browser session ready")

    async def navigate_to_detail_page(self, page, detail_url: str, bar_number: str) -> None:
        """
        NC Bar's viewer.aspx validates that the requested ID exists in the current
        session's search result set — direct navigation always fails.

        Strategy: search by bar number → wait for results → click the name link.
        This populates the session result set so viewer.aspx accepts the request.
        """
        logger.debug(f"NC Bar: searching for bar_number={bar_number} to load detail page")

        # Navigate to a fresh search form
        await page.goto(
            "https://portal.ncbar.gov/Verification/search.aspx",
            wait_until="domcontentloaded",
            timeout=30000,
        )
        await page.wait_for_timeout(500)

        # Fill the State Bar ID / licence number field
        lic_input = await page.query_selector('#txtLicNum')
        if lic_input:
            await lic_input.fill(bar_number)
        else:
            logger.warning(f"NC Bar: #txtLicNum field not found for bar_number={bar_number}")

        # Submit the search
        btn = await page.query_selector('#btnSubmit')
        if btn:
            await btn.click()
        else:
            fallback = await page.query_selector('input[type="submit"]')
            if fallback:
                await fallback.click()

        # Wait for results page
        await page.wait_for_load_state("domcontentloaded", timeout=20000)
        await page.wait_for_timeout(800)

        # Click the first name link in the results table
        link = await page.query_selector('table.table-hover tr td:nth-child(2) a')
        if link:
            await link.click()
            await page.wait_for_load_state("domcontentloaded", timeout=15000)
            logger.debug(f"NC Bar: detail page reached via search, url={page.url}")
        else:
            # No results found — attorney may be inactive or bar number wrong
            logger.warning(
                f"NC Bar: no result link found after searching bar_number={bar_number}, "
                "falling back to direct navigation"
            )
            await page.goto(detail_url, wait_until="domcontentloaded", timeout=15000)

    # ── Public API ────────────────────────────────────────────────────────────────

    async def parse_detail_page(
        self,
        page: Page,
        bar_number: str,
        existing_data: Optional[LawyerRawData] = None,
    ) -> Optional[LawyerRawData]:
        """
        Parse an NC Bar member detail page using Bootstrap definition list structure.
        """
        logger.debug(f"Parsing NC Bar detail page for bar_number={bar_number}")
        await self.wait_for_page_load(page)

        # Check if redirected to error page
        current_url = page.url
        if 'error.html' in current_url:
            logger.warning(f"NC Bar profile not found for bar_number={bar_number}, redirected to error page")
            return None

        # Get page HTML and parse with BeautifulSoup
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")

        # Check for error page content
        h1 = soup.find("h1")
        if h1 and "error" in h1.get_text(strip=True).lower():
            logger.warning(f"NC Bar profile not found for bar_number={bar_number}, page shows error")
            return None

        # Parse the definition list
        data = self._parse_definition_list(soup)

        # Also try to get name from panel heading as fallback
        panel_heading = soup.find("div", class_="panel-heading")
        if panel_heading:
            heading_text = panel_heading.get_text(strip=True)
            # Format: "Ms. Morgan Parker Abbott - Attorney"
            if " - " in heading_text:
                name_from_heading = heading_text.split(" - ")[0].strip()
                if name_from_heading and not data.get("name"):
                    data["name"] = name_from_heading

        # Extract fields
        name_raw = data.get("name")
        bar_id = data.get("bar_id") or bar_number
        status = data.get("status")
        admitted = data.get("date_admitted")
        city = data.get("city")
        state_val = data.get("state")
        zip_code = data.get("zip_code")
        phone = data.get("work_phone")
        email = data.get("email")
        judicial_dist = data.get("judicial_district")

        # Build address from components
        address_raw = data.get("address")
        address = self._build_address_from_raw(address_raw, city, state_val, zip_code)

        # Clean name (remove honorifics)
        full_name = self._clean_name(name_raw) if name_raw else (
            existing_data.full_name if existing_data else None
        )

        # Extract admission year
        admission_year = self._extract_year(admitted)

        return LawyerRawData(
            full_name=full_name,
            bar_number=bar_id or bar_number,
            firm_name=None,  # NC Bar doesn't show firm in detail page
            address=address or (existing_data.address if existing_data else None),
            phone=phone,
            fax=None,  # Not shown on detail page
            email=email,
            practice_areas=None,  # Board certified section is usually empty
            admission_year=admission_year,
            license_status=status or (existing_data.license_status if existing_data else None),
            detail_url=self.construct_detail_url(bar_number),
        )

    def _parse_definition_list(self, soup: BeautifulSoup) -> dict:
        """
        Parse the Bootstrap definition list (<dl class="dl-horizontal">).

        Returns a dict with field names mapped to values.
        """
        data = {}

        # Find all definition lists
        dl_lists = soup.find_all("dl", class_="dl-horizontal")

        for dl in dl_lists:
            dts = dl.find_all("dt")
            dds = dl.find_all("dd")

            for dt, dd in zip(dts, dds):
                label = dt.get_text(strip=True).rstrip(":").lower()
                # Get value, handling <br> tags in address
                value = dd.get_text("\n", strip=True)
                # Also get raw HTML for address parsing
                value_html = str(dd)

                # Map labels to field names
                if "bar #" in label or "bar id" in label:
                    data["bar_id"] = value
                elif label == "name":
                    data["name"] = value
                elif label == "address":
                    data["address"] = value_html  # Keep HTML for <br> parsing
                elif label == "city":
                    data["city"] = value
                elif label == "state":
                    data["state"] = value
                elif "zip" in label:
                    data["zip_code"] = value
                elif "phone" in label:
                    data["work_phone"] = value
                elif label == "email":
                    data["email"] = value
                elif label == "status":
                    # Extract status from span if present
                    status_span = dd.find("span", class_="label")
                    if status_span:
                        data["status"] = status_span.get_text(strip=True)
                    else:
                        data["status"] = value
                elif "admitted" in label:
                    data["date_admitted"] = value
                elif "judicial" in label:
                    data["judicial_district"] = value

        return data

    def _build_address_from_raw(
        self,
        address_raw: Optional[str],
        city: Optional[str],
        state: Optional[str],
        zip_code: Optional[str],
    ) -> Optional[str]:
        """
        Build a complete address from components.

        address_raw may contain HTML with <br> tags for multi-line addresses.
        """
        parts = []

        if address_raw:
            # Parse HTML to extract address lines
            soup = BeautifulSoup(address_raw, "html.parser")
            # Get text with newlines for <br> tags
            address_text = soup.get_text("\n").strip()
            # Split by newlines and add non-empty parts
            for line in address_text.split("\n"):
                line = line.strip()
                if line:
                    parts.append(line)

        # Add city, state, zip
        city_state_zip = ", ".join(filter(None, [city, state]))
        if zip_code:
            city_state_zip = f"{city_state_zip} {zip_code}".strip()
        if city_state_zip:
            parts.append(city_state_zip)

        return ", ".join(parts) if parts else None

    def construct_detail_url(self, bar_number: str) -> str:
        """NC Bar viewer URL pattern."""
        return f"{self.BASE_URL}/Verification/viewer.aspx?ID={bar_number}"

    async def expand_additional_info(self, page: Page) -> bool:
        """NC Bar pages are fully expanded by default."""
        return False

    # ── Abstract method implementations (required by BaseDetailEnricher) ─────────

    async def _extract_address(self, page: Page) -> Optional[str]:
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        data = self._parse_definition_list(soup)
        return self._build_address_from_raw(
            data.get("address"),
            data.get("city"),
            data.get("state"),
            data.get("zip_code")
        )

    async def _extract_phone(self, page: Page) -> Optional[str]:
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        data = self._parse_definition_list(soup)
        return data.get("work_phone")

    async def _extract_fax(self, page: Page) -> Optional[str]:
        return None  # Not shown on NC Bar detail page

    async def _extract_email(self, page: Page) -> Optional[str]:
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        data = self._parse_definition_list(soup)
        return data.get("email")

    async def _extract_website(self, page: Page) -> Optional[str]:
        return None  # NC Bar doesn't expose a website field

    async def _extract_law_school(self, page: Page) -> Optional[str]:
        return None  # NC Bar doesn't publish law school on member pages

    async def wait_for_page_load(self, page: Page) -> None:
        """Wait for NC Bar detail page content to be available."""
        try:
            # Wait for the verification body panel which contains attorney data
            await page.wait_for_selector('#pnlVerificationBody', timeout=15000)
        except Exception:
            try:
                await page.wait_for_load_state('domcontentloaded', timeout=10000)
            except Exception:
                pass
        await page.wait_for_timeout(500)

    # ── Private helpers ───────────────────────────────────────────────────────────

    @staticmethod
    def _clean_name(raw: str) -> str:
        """
        Strip honorifics from a name string.

        Examples:
            "Ms. Morgan Parker Abbott" → "Morgan Parker Abbott"
            "Mr. John Smith" → "John Smith"
        """
        if not raw:
            return raw
        # Remove leading honorifics
        cleaned = re.sub(
            r'^(?:Mr\.|Mrs\.|Ms\.|Dr\.|Prof\.|Judge|Hon\.|Sir|Lady)\s+',
            '',
            raw.strip(),
            flags=re.I,
        )
        return cleaned.strip()

    @staticmethod
    def _extract_year(text: Optional[str]) -> Optional[int]:
        """Extract a 4-digit year from an admission date string."""
        if not text:
            return None
        m = re.search(r'\b(19|20)\d{2}\b', text)
        return int(m.group(0)) if m else None
