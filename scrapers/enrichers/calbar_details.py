"""California State Bar detail page enricher.

Actual page HTML structure (apps.calbar.ca.gov/attorney/Licensee/Detail/{bar}):
    <h3 class="donotprint"><b>Attorney Profile</b></h3>
    <div>
      <h3><b>Bonnie Joy Cooper Zahn #108715</b></h3>
      <div><p><b>License Status: Active</b></p></div>
    </div>
    <div>
      <p>Address: 2539 E 7th St, Long Beach, CA 90804-4646</p>
      <p>Phone: 562-881-7251 | Fax: Not Available</p>
      <p>Email: [obfuscated honeypot spans - only one visible] | Website: Not Available</p>
      <a>More about This Attorney</a>
    </div>
    <!-- After expanding "More about" -->
    <div>CLA Sections: ...</div>
    <div>Self-Reported Practice Areas: ...</div>
    <div>Additional Languages Spoken: ...</div>
    <div>Law School: ...</div>

Note: Email field uses anti-scraping obfuscation with ~20 decoy mailto: spans.
      Only the visible text (via inner_text) shows the real email.
"""

import re
from typing import Optional, Dict, List
from playwright.async_api import Page
from scrapers.enrichers.base_detail import BaseDetailEnricher
from scrapers.base import LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


class CaliforniaDetailEnricher(BaseDetailEnricher):
    """
    California State Bar detail page enricher.

    Scrapes lawyer detail pages from apps.calbar.ca.gov to extract:
    - Contact information (phone, fax, email, website)
    - Practice areas
    - Education (law school)
    - Languages spoken
    - CLA sections/memberships
    - Normalized name from detail page
    """

    BASE_URL = "https://apps.calbar.ca.gov"
    STATE_CODE = "CA"
    SOURCE = "calbar_details"

    async def parse_detail_page(
        self,
        page: Page,
        bar_number: str,
        existing_data: Optional[LawyerRawData] = None
    ) -> LawyerRawData:
        """Parse California State Bar detail page."""
        logger.debug(f"Parsing detail page for bar number: {bar_number}")

        await self.wait_for_page_load(page)

        # Get the full visible text of the page body (CSS-aware, hides decoy emails)
        body_text = await page.inner_text('body')

        # Extract name from the heading containing #bar_number
        name_info = await self._extract_name(page, bar_number)

        # Extract license status
        license_status = self._parse_license_status(body_text)

        # Extract contact info from the visible text
        address = self._parse_address(body_text)
        phone = self._parse_phone(body_text)
        fax = self._parse_fax(body_text)
        email = self._parse_email(body_text)
        website = self._parse_website(body_text)

        # Extract firm name from address
        firm_name = self._parse_firm_name(address)

        # Expand "More About This Attorney" section
        expanded = await self.expand_additional_info(page)
        if expanded:
            logger.debug(f"Expanded additional info for bar {bar_number}")
            # Re-read body text after expansion to get practice areas, languages, etc.
            body_text = await page.inner_text('body')

        # Extract fields from expanded section
        practice_areas = self._parse_practice_areas(body_text)
        law_school = self._parse_law_school(body_text)
        admission_year = self._parse_admission_year(body_text)
        languages = self._parse_languages(body_text)

        return LawyerRawData(
            full_name=name_info or (existing_data.full_name if existing_data else None),
            bar_number=bar_number,
            firm_name=firm_name or (existing_data.firm_name if existing_data else None),
            address=address or (existing_data.address if existing_data else None),
            phone=phone,
            fax=fax,
            email=email,
            website_url=website,
            practice_areas=practice_areas if practice_areas else None,
            law_school=law_school,
            admission_year=admission_year,
            license_status=license_status or (existing_data.license_status if existing_data else None),
            detail_url=self.construct_detail_url(bar_number),
        )

    # ── DOM-based extraction (needs actual page object) ─────────────

    async def _extract_name(self, page: Page, bar_number: str) -> Optional[str]:
        """
        Extract full name from the h3 heading that contains #bar_number.

        HTML: <h3><b>Bonnie Joy Cooper Zahn #108715</b></h3>
        """
        try:
            # Find the heading containing the bar number
            name_el = await page.query_selector(f'h3:has-text("#{bar_number}")')
            if name_el:
                text = await name_el.inner_text()
                # Remove bar number suffix: "Bonnie Joy Cooper Zahn #108715" -> "Bonnie Joy Cooper Zahn"
                name = re.sub(r'\s*#\d+\s*$', '', text).strip()
                if name:
                    logger.debug(f"Extracted name: {name}")
                    return name

            logger.warning("Could not find name heading with bar number")
            return None
        except Exception as e:
            logger.error(f"Failed to extract name: {e}")
            return None

    async def expand_additional_info(self, page: Page) -> bool:
        """Click 'More about This Attorney' to reveal practice areas, languages, education."""
        try:
            more_link = await page.query_selector('a:has-text("More about This Attorney")')
            if more_link:
                await more_link.click()
                await page.wait_for_timeout(1000)
                logger.debug("Expanded 'More about This Attorney' section")
                return True

            logger.debug("'More about This Attorney' link not found")
            return False
        except Exception as e:
            logger.warning(f"Failed to expand additional info: {e}")
            return False

    # ── Text-based parsing (works on visible body text) ─────────────

    def _parse_license_status(self, text: str) -> Optional[str]:
        """Extract 'License Status: Active' from body text."""
        match = re.search(r'License Status:\s*(\w+)', text)
        return match.group(1) if match else None

    def _parse_address(self, text: str) -> Optional[str]:
        """
        Extract address line from body text.

        The address line is: "Address: 2539 E 7th St, Long Beach, CA 90804-4646"
        It ends before the next line (Phone: ...).
        """
        match = re.search(r'Address:\s*(.+?)(?:\n|$)', text)
        if match:
            addr = match.group(1).strip()
            return addr if addr else None
        return None

    def _parse_phone(self, text: str) -> Optional[str]:
        """Extract phone number. Format: 'Phone: 562-881-7251'"""
        match = re.search(r'Phone:\s*([\d()-]+[\d-]+)', text)
        if match:
            phone = match.group(1).strip()
            # Filter out "Not Available"
            return phone if phone and phone[0].isdigit() else None
        return None

    def _parse_fax(self, text: str) -> Optional[str]:
        """Extract fax number. Format: 'Fax: 415-395-8095' or 'Fax: Not Available'"""
        match = re.search(r'Fax:\s*([\d()-]+[\d-]+)', text)
        if match:
            fax = match.group(1).strip()
            return fax if fax and fax[0].isdigit() else None
        return None

    def _parse_email(self, text: str) -> Optional[str]:
        """
        Extract email from visible body text.

        CA State Bar uses email obfuscation with ~20 decoy mailto spans.
        CSS hides all but the real email. page.inner_text() returns only
        the visible text, so we can safely parse the email from it.
        """
        # Look for "Email: someone@example.com" in the visible text
        match = re.search(r'Email:\s*([\w\.\-+]+@[\w\.\-]+\.\w+)', text)
        if match:
            email = match.group(1).strip()
            return email
        return None

    def _parse_website(self, text: str) -> Optional[str]:
        """Extract website URL. Returns None if 'Not Available'."""
        match = re.search(r'Website:\s*(.+?)(?:\n|$)', text)
        if match:
            website = match.group(1).strip()
            if 'not available' in website.lower():
                return None
            # Extract URL if present
            url_match = re.search(r'https?://\S+', website)
            return url_match.group(0) if url_match else (website if website else None)
        return None

    def _parse_firm_name(self, address: Optional[str]) -> Optional[str]:
        """
        Extract firm name from the first part of the address, if it looks like a firm.

        Only returns a firm name if it contains recognized legal entity suffixes.
        Example: "Latham & Watkins LLP, 505 Montgomery St" -> "Latham & Watkins LLP"
        """
        if not address:
            return None
        parts = address.split(',')
        if len(parts) > 1:
            candidate = parts[0].strip()
            suffixes = ['LLP', 'LLC', 'PC', 'PA', 'INC', 'PLLC', 'LAW', 'P.C.', 'L.L.P.', 'P.A.']
            if any(suffix in candidate.upper() for suffix in suffixes):
                return candidate
        return None

    def _parse_practice_areas(self, text: str) -> List[str]:
        """Extract practice areas from 'Self-Reported Practice Areas:' section."""
        match = re.search(
            r'Self-Reported Practice Areas:\s*(.+?)(?=Additional Languages|Law School|License Status,|$)',
            text,
            re.DOTALL
        )
        if match:
            areas_text = match.group(1).strip()
            if 'none reported' in areas_text.lower():
                return []
            # Split by newlines or commas
            areas = re.split(r'\n|,\s*', areas_text)
            # Filter out disclaimer text and empty lines
            skip_phrases = [
                'none reported', 'note:', 'state bar', 'does not verify',
                'warranties', 'certified specialist', 'lawyer referral',
                'lawhelpca', 'online public information', 'complement',
                'encourages', 'experience or competence',
            ]
            cleaned = []
            for a in areas:
                a = a.strip()
                if not a:
                    continue
                lower = a.lower()
                if any(phrase in lower for phrase in skip_phrases):
                    continue
                cleaned.append(a)
            return cleaned
        return []

    def _parse_law_school(self, text: str) -> Optional[str]:
        """Extract law school. Format: 'Law School: Loyola Law School; Los Angeles CA'"""
        match = re.search(r'Law School:\s*(.+?)(?:\n|$)', text)
        if match:
            school = match.group(1).strip()
            return school if school else None
        return None

    def _parse_admission_year(self, text: str) -> Optional[int]:
        """
        Extract admission year from the history table.

        The page contains: "6/3/1983 Admitted to the State Bar of California"
        """
        match = re.search(r'(\d{1,2}/\d{1,2}/(\d{4}))\s+Admitted to the State Bar', text)
        if match:
            return int(match.group(2))
        return None

    def _parse_languages(self, text: str) -> List[str]:
        """Extract languages from 'Additional Languages Spoken:' section."""
        match = re.search(
            r'Additional Languages Spoken:\s*(.+?)(?=Law School|License Status,|$)',
            text,
            re.DOTALL
        )
        if match:
            lang_block = match.group(1).strip()
            # Extract "By the attorney:" section
            attorney_match = re.search(r'By the attorney:\s*(.+?)(?=By staff:|$)', lang_block, re.DOTALL)
            if attorney_match:
                lang_text = attorney_match.group(1).strip()
                if 'none reported' in lang_text.lower():
                    return []
                langs = re.split(r'[,;\n]', lang_text)
                return [l.strip() for l in langs if l.strip()]
        return []

    # ── Required abstract method implementations ────────────────────
    # These delegate to the text-based parsers above via extract_contact_info

    async def normalize_name(self, page: Page) -> Dict[str, Optional[str]]:
        """Not used directly — parse_detail_page calls _extract_name instead."""
        return {'full_name': None, 'first_name': None, 'last_name': None}

    async def _extract_address(self, page: Page) -> Optional[str]:
        text = await page.inner_text('body')
        return self._parse_address(text)

    async def _extract_phone(self, page: Page) -> Optional[str]:
        text = await page.inner_text('body')
        return self._parse_phone(text)

    async def _extract_fax(self, page: Page) -> Optional[str]:
        text = await page.inner_text('body')
        return self._parse_fax(text)

    async def _extract_email(self, page: Page) -> Optional[str]:
        text = await page.inner_text('body')
        return self._parse_email(text)

    async def _extract_website(self, page: Page) -> Optional[str]:
        text = await page.inner_text('body')
        return self._parse_website(text)

    async def _extract_law_school(self, page: Page) -> Optional[str]:
        text = await page.inner_text('body')
        return self._parse_law_school(text)
