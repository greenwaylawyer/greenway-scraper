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
from bs4 import BeautifulSoup
from playwright.async_api import Page
from scrapers.enrichers.base_detail import BaseDetailEnricher
from scrapers.base import LawyerRawData
from normalizers.text import title_law_school
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

        # Parse both rendered text and raw HTML. The CalBar page contains useful
        # detail content inside hidden DOM blocks, so HTML parsing is more reliable
        # than relying on visible text alone.
        body_text = await self._get_page_text(page)
        html = await page.content()
        soup = BeautifulSoup(html, 'html.parser')

        # Extract name from the heading containing #bar_number
        name_info = await self._extract_name(page, bar_number)

        # Extract license status
        license_status = self._parse_license_status_from_html(soup) or self._parse_license_status(body_text)

        # Extract contact info primarily from HTML, with text fallback.
        address = self._parse_address_from_html(soup) or self._parse_address(body_text)
        phone = self._parse_phone_from_html(soup) or self._parse_phone(body_text)
        fax = self._parse_fax_from_html(soup) or self._parse_fax(body_text)
        email = self._parse_email_from_html(soup, name_info or (existing_data.full_name if existing_data else None)) or self._parse_email(body_text)
        website = self._parse_website_from_html(soup) or self._parse_website(body_text)

        # Extract firm name from address
        firm_name = self._parse_firm_name(address)

        # The "More about This Attorney" content is already present in the HTML,
        # usually just hidden inside the Kendo panel. Parse it directly from HTML.
        expanded = False
        practice_areas = self._parse_practice_areas_from_html(soup)
        if not practice_areas:
            expanded = await self.expand_additional_info(page)
            if expanded:
                logger.debug(f"Expanded additional info for bar {bar_number}")
                body_text = await self._get_page_text(page)
                html = await page.content()
                soup = BeautifulSoup(html, 'html.parser')
                practice_areas = self._parse_practice_areas_from_html(soup)

        law_school = self._parse_law_school_from_html(soup) or self._parse_law_school(body_text)
        admission_year = self._parse_admission_year(body_text)
        languages = self._parse_languages_from_html(soup) or self._parse_languages(body_text)

        missing_fields = []
        if not address:
            missing_fields.append('address')
        if not phone:
            missing_fields.append('phone')
        if not email:
            missing_fields.append('email')
        if not law_school:
            missing_fields.append('law_school')

        logger.info(
            "CA detail parse result",
            bar_number=bar_number,
            has_address=bool(address),
            has_phone=bool(phone),
            has_email=bool(email),
            has_fax=bool(fax),
            has_website=bool(website),
            has_law_school=bool(law_school),
            practice_count=len(practice_areas),
            expanded_more_about=expanded,
            missing_fields=missing_fields,
        )

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
            selectors = [
                'a:has-text("More about This Attorney")',
                'a:has-text("More about this attorney")',
                'button:has-text("More about This Attorney")',
                'a[aria-controls*="more"]',
                'a[href*="more"]',
            ]
            for selector in selectors:
                more_link = await page.query_selector(selector)
                if not more_link:
                    continue

                try:
                    await more_link.click(timeout=3000)
                except Exception:
                    await more_link.evaluate("el => el.click()")

                await page.wait_for_timeout(1000)
                logger.debug("Expanded 'More about This Attorney' section", selector=selector)
                return True

            logger.debug("'More about This Attorney' link not found")
            return False
        except Exception as e:
            logger.warning(f"Failed to expand additional info: {e}")
            return False

    async def _get_page_text(self, page: Page) -> str:
        """Return stable parse text from page with fallback strategies."""
        try:
            text = await page.inner_text('body')
            if text and len(text.strip()) > 100:
                return text
        except Exception:
            pass

        try:
            text = await page.text_content('body')
            return text or ""
        except Exception:
            return ""

    # ── Text-based parsing (works on visible body text) ─────────────

    def _parse_license_status(self, text: str) -> Optional[str]:
        """Extract 'License Status: Active' from body text."""
        match = re.search(r'License Status:\s*([^\n|]+)', text)
        return match.group(1).strip() if match else None

    def _parse_license_status_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        text = soup.get_text("\n", strip=True)
        return self._parse_license_status(text)

    def _parse_address(self, text: str) -> Optional[str]:
        """
        Extract address line from body text.

        The address line is: "Address: 2539 E 7th St, Long Beach, CA 90804-4646"
        It ends before the next line (Phone: ...).
        """
        match = re.search(r'Address:\s*(.+?)(?=\n(?:Phone:|Email:|Website:|Fax:)|$)', text, re.DOTALL)
        if match:
            addr = re.sub(r'\s+', ' ', match.group(1)).strip()
            return addr if addr else None
        return None

    def _parse_address_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        for p in soup.find_all('p'):
            text = p.get_text(' ', strip=True)
            if text.startswith('Address:'):
                return re.sub(r'\s+', ' ', text.replace('Address:', '', 1)).strip() or None
        return None

    def _parse_phone(self, text: str) -> Optional[str]:
        """Extract phone number. Format: 'Phone: 562-881-7251'"""
        match = re.search(r'Phone:\s*([\d()\-\.\s]+\d)', text)
        if match:
            phone = match.group(1).strip()
            # Filter out "Not Available"
            return phone if phone and phone[0].isdigit() else None
        return None

    def _parse_phone_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        text = soup.get_text('\n', strip=True)
        return self._parse_phone(text)

    def _parse_fax(self, text: str) -> Optional[str]:
        """Extract fax number. Format: 'Fax: 415-395-8095' or 'Fax: Not Available'"""
        match = re.search(r'Fax:\s*([\d()\-\.\s]+\d)', text)
        if match:
            fax = match.group(1).strip()
            return fax if fax and fax[0].isdigit() else None
        return None

    def _parse_fax_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        text = soup.get_text('\n', strip=True)
        return self._parse_fax(text)

    def _parse_email(self, text: str) -> Optional[str]:
        """
        Extract email from visible body text.

        CA State Bar uses email obfuscation with ~20 decoy mailto spans.
        CSS hides all but the real email. page.inner_text() returns only
        the visible text, so we can safely parse the email from it.
        """
        # Look for "Email: someone@example.com" in the visible text
        match = re.search(r'Email:\s*([\w\.\-+]+@[\w\.\-]+\.\w+)', text)
        if not match:
            # Basic normalization for obfuscated variants in visible text.
            normalized = text.replace('[at]', '@').replace('(at)', '@').replace(' at ', '@')
            normalized = normalized.replace('[dot]', '.').replace('(dot)', '.').replace(' dot ', '.')
            match = re.search(r'Email:\s*([\w\.\-+]+@[\w\.\-]+\.\w+)', normalized)
        if match:
            email = match.group(1).strip()
            return email
        return None

    def _parse_email_from_html(self, soup: BeautifulSoup, full_name: Optional[str]) -> Optional[str]:
        email_line = None
        for p in soup.find_all('p'):
            if 'Email:' in p.get_text(' ', strip=True):
                email_line = p
                break

        if not email_line:
            return None

        candidates: list[str] = []
        for span in email_line.find_all('span'):
            text = span.get_text('', strip=True).replace(' ', '')
            if re.fullmatch(r'[\w.\-+]+@[\w.\-]+\.\w+', text):
                candidates.append(text)

        if not candidates:
            line_text = email_line.get_text(' ', strip=True)
            return self._parse_email(line_text)

        # Pick the most human-looking candidate. Real emails tend to contain
        # name fragments or common domains; decoys tend to be random strings.
        name_tokens = []
        if full_name:
            name_tokens = [token.lower() for token in re.findall(r'[A-Za-z]{3,}', full_name)]

        def score(email: str) -> int:
            local, _, domain = email.lower().partition('@')
            domain_name = domain.split('.')[0]
            score_value = 0
            for token in name_tokens:
                if token in local:
                    score_value += 5
                if token in domain_name:
                    score_value += 3
            for provider in ('gmail', 'yahoo', 'hotmail', 'outlook', 'icloud', 'aol'):
                if provider in domain:
                    score_value += 4
            if any(ch.isdigit() for ch in local):
                score_value += 1
            if re.search(r'[bcdfghjklmnpqrstvwxyz]{6,}', local):
                score_value -= 3
            if re.search(r'[bcdfghjklmnpqrstvwxyz]{6,}', domain_name):
                score_value -= 3
            if len(local) <= 3:
                score_value -= 2
            return score_value

        ranked = sorted(candidates, key=score, reverse=True)
        return ranked[0] if ranked else None

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

    def _parse_website_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        for p in soup.find_all('p'):
            text = p.get_text(' ', strip=True)
            if 'Website:' not in text:
                continue
            link = p.find('a', href=True)
            if link and link['href']:
                return link['href']
            return self._parse_website(text)
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

    def _parse_practice_areas_from_html(self, soup: BeautifulSoup) -> List[str]:
        strong = soup.find('strong', string=re.compile(r'Self-Reported Practice Areas:', re.I))
        if not strong:
            return []

        container = strong.find_parent('div') or strong.parent
        if not container:
            return []

        ul = container.find('ul')
        if not ul:
            text = container.get_text(' ', strip=True)
            if 'None reported' in text:
                return []
            return self._parse_practice_areas(text)

        results = []
        for li in ul.find_all('li'):
            value = re.sub(r'\s+', ' ', li.get_text(' ', strip=True)).strip()
            if value and 'none reported' not in value.lower():
                results.append(value)
        return results

    def _parse_law_school(self, text: str) -> Optional[str]:
        """Extract law school. Format: 'Law School: Loyola Law School; Los Angeles CA'"""
        match = re.search(r'Law School:\s*(.+?)(?:\n|$)', text, re.DOTALL)
        if match:
            school = re.sub(r'\s+', ' ', match.group(1)).strip()
            return title_law_school(school) if school else None
        return None

    def _parse_law_school_from_html(self, soup: BeautifulSoup) -> Optional[str]:
        strong = soup.find('strong', string=re.compile(r'Law School:', re.I))
        if not strong:
            return None

        parent = strong.parent
        if not parent:
            return None

        text = parent.get_text(' ', strip=True)
        return self._parse_law_school(text)

    def _parse_admission_year(self, text: str) -> Optional[int]:
        """
        Extract admission year from the history table.

        The page contains: "6/3/1983 Admitted to the State Bar of California"
        """
        match = re.search(r'(\d{1,2}/\d{1,2}/(\d{4})).{0,100}Admitted to the State Bar', text, re.DOTALL)
        if match:
            return int(match.group(2))

        if 'Admitted to the State Bar' in text:
            years = re.findall(r'\b(19\d{2}|20\d{2})\b', text)
            if years:
                return int(years[0])
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

    def _parse_languages_from_html(self, soup: BeautifulSoup) -> List[str]:
        strong = soup.find('strong', string=re.compile(r'Additional Languages Spoken:', re.I))
        if not strong:
            return []

        container = strong.find_parent('div') or strong.parent
        if not container:
            return []

        items = []
        for li in container.find_all('li'):
            text = re.sub(r'\s+', ' ', li.get_text(' ', strip=True)).strip()
            if not text or 'none reported' in text.lower():
                continue
            text = re.sub(r'^By the attorney:\s*', '', text, flags=re.I)
            text = re.sub(r'^By staff:\s*', '', text, flags=re.I)
            if text:
                items.append(text)
        return items

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
