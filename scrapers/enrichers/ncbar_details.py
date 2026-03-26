"""North Carolina State Bar detail page enricher.

Typical NC Bar detail page URL:
    https://portal.ncbar.gov/Verification/MemberDetail.aspx?id=<STATE_BAR_ID>

Typical page structure (ASP.NET WebForms):
    <span id="...lblName">         Smith, John A                  </span>
    <span id="...lblBarID">        12345                          </span>
    <span id="...lblStatus">       Active                         </span>
    <span id="...lblAdmitted">     1995                           </span>
    <span id="...lblType">         Attorney                       </span>
    <span id="...lblFirm">         Smith & Associates PLLC        </span>
    <span id="...lblAddress1">     123 Main St                    </span>
    <span id="...lblAddress2">     Suite 400                      </span>
    <span id="...lblCity">         Charlotte                      </span>
    <span id="...lblState">        NC                             </span>
    <span id="...lblZip">          28202                          </span>
    <span id="...lblCounty">       Mecklenburg                    </span>
    <span id="...lblJudicialDist"> 26th Judicial District         </span>
    <span id="...lblPhone">        704-555-1234                   </span>
    <span id="...lblEmail">        john@example.com               </span>
    <!-- Board Certified section may be absent for non-specialists -->
    <span id="...lblBoardCert">    Family Law Specialist          </span>

Note: actual ASP.NET content-placeholder prefixes vary; we use id$= suffix
selectors so all variants are matched robustly.
"""

import re
from typing import Optional

from playwright.async_api import Page

from scrapers.enrichers.base_detail import BaseDetailEnricher
from scrapers.base import LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


class NorthCarolinaDetailEnricher(BaseDetailEnricher):
    """
    Enricher for NC Bar member detail pages.

    Scrapes https://portal.ncbar.gov/Verification/MemberDetail.aspx?id=<BAR_ID>
    and returns a populated LawyerRawData.
    """

    BASE_URL = "https://portal.ncbar.gov"
    STATE_CODE = "NC"
    SOURCE = "ncbar_details"

    # ── ASP.NET label selectors (suffix-match to survive master-page prefixes) ───
    # Format: (field_name, css_selector_list)
    # Multiple selectors are tried in order until one has non-empty text.
    SELECTORS = {
        "name":            ['[id$="lblName"]',           '[id$="lblFullName"]',      '[id$="Name"]'],
        "bar_id":          ['[id$="lblBarID"]',          '[id$="lblBarNumber"]',     '[id$="BarID"]'],
        "status":          ['[id$="lblStatus"]',         '[id$="lblMemberStatus"]',  '[id$="Status"]'],
        "admitted":        ['[id$="lblAdmitted"]',       '[id$="lblAdmitDate"]',     '[id$="Admitted"]'],
        "member_type":     ['[id$="lblType"]',           '[id$="lblMemberType"]'],
        "firm":            ['[id$="lblFirm"]',           '[id$="lblOrganization"]',  '[id$="Firm"]'],
        "address1":        ['[id$="lblAddress1"]',       '[id$="lblAdd1"]',          '[id$="Address1"]'],
        "address2":        ['[id$="lblAddress2"]',       '[id$="lblAdd2"]',          '[id$="Address2"]'],
        "city":            ['[id$="lblCity"]'],
        "state":           ['[id$="lblState"]'],
        "zip":             ['[id$="lblZip"]',            '[id$="lblZipCode"]'],
        "county":          ['[id$="lblCounty"]'],
        "judicial_dist":   ['[id$="lblJudicialDistrict"]', '[id$="lblJudDist"]'],
        "phone":           ['[id$="lblPhone"]',          '[id$="lblPhoneNumber"]'],
        "fax":             ['[id$="lblFax"]',            '[id$="lblFaxNumber"]'],
        "email":           ['[id$="lblEmail"]',          '[id$="lblEmailAddress"]'],
        "board_cert":      ['[id$="lblBoardCert"]',      '[id$="lblBoardCertified"]', '[id$="BoardCert"]'],
    }

    # ── Public API ────────────────────────────────────────────────────────────────

    async def parse_detail_page(
        self,
        page: Page,
        bar_number: str,
        existing_data: Optional[LawyerRawData] = None,
    ) -> LawyerRawData:
        """
        Parse an NC Bar member detail page.

        Falls back to full-page text parsing when label selectors miss,
        ensuring we still extract data even if the portal updates its HTML.
        """
        logger.debug(f"Parsing NC Bar detail page for bar_number={bar_number}")
        await self.wait_for_page_load(page)

        # ── Try label-based extraction first ─────────────────────────────────────
        name_raw    = await self._text(page, "name")
        bar_id      = await self._text(page, "bar_id") or bar_number
        status      = await self._text(page, "status")
        admitted    = await self._text(page, "admitted")
        firm        = await self._text(page, "firm")
        address1    = await self._text(page, "address1")
        address2    = await self._text(page, "address2")
        city        = await self._text(page, "city")
        state_val   = await self._text(page, "state")
        zip_code    = await self._text(page, "zip")
        phone       = await self._text(page, "phone")
        fax         = await self._text(page, "fax")
        email       = await self._text(page, "email")
        board_cert  = await self._text(page, "board_cert")
        judicial    = await self._text(page, "judicial_dist")

        # ── Fall back to body-text parsing when label lookup fails ────────────────
        body_text = ""
        if not name_raw or not address1:
            body_text = await page.inner_text("body")

        if not name_raw:
            name_raw = self._parse_body_field(body_text, r"Name[:\s]+(.+)")
        if not status:
            status = self._parse_body_field(body_text, r"(?:Member\s+)?Status[:\s]+(\w[\w\s]*?)(?:\n|$)")
        if not firm:
            firm = self._parse_body_field(body_text, r"(?:Firm|Organization)[:\s]+(.+?)(?:\n|$)")
        if not phone:
            phone = self._parse_body_field(body_text, r"Phone[:\s]+([\d()\-\s\.ext]+\d)")
        if not fax:
            fax = self._parse_body_field(body_text, r"Fax[:\s]+([\d()\-\s\.]+\d)")
        if not email:
            email = self._parse_body_field(body_text, r"Email[:\s]+([\w.+\-]+@[\w.\-]+\.\w+)")

        # ── Assemble full address ─────────────────────────────────────────────────
        address = self._build_address(address1, address2, city, state_val, zip_code)
        if not address and body_text:
            address = self._parse_address_from_body(body_text)

        # ── Name normalisation ────────────────────────────────────────────────────
        # NC Bar stores names as "Last, First Middle" – we convert to "First Last"
        full_name = self._normalise_nc_name(name_raw) if name_raw else (
            existing_data.full_name if existing_data else None
        )

        # ── Admission year ────────────────────────────────────────────────────────
        admission_year = self._extract_year(admitted)

        # ── Practice areas (board certified) ─────────────────────────────────────
        practice_areas = None
        if board_cert and board_cert.lower() not in ("n/a", "none", ""):
            practice_areas = [board_cert.strip()]

        return LawyerRawData(
            full_name=full_name,
            bar_number=bar_id or bar_number,
            firm_name=firm or (existing_data.firm_name if existing_data else None),
            address=address or (existing_data.address if existing_data else None),
            phone=phone,
            fax=fax,
            email=email,
            practice_areas=practice_areas,
            admission_year=admission_year,
            license_status=status or (existing_data.license_status if existing_data else None),
            detail_url=self.construct_detail_url(bar_number),
        )

    def construct_detail_url(self, bar_number: str) -> str:
        """NC Bar member detail URL pattern."""
        return f"{self.BASE_URL}/Verification/MemberDetail.aspx?id={bar_number}"

    async def expand_additional_info(self, page: Page) -> bool:
        """
        NC Bar pages are fully expanded by default – no accordion/toggle sections.
        """
        return False

    # ── Abstract method implementations (required by BaseDetailEnricher) ─────────

    async def _extract_address(self, page: Page) -> Optional[str]:
        a1 = await self._text(page, "address1")
        a2 = await self._text(page, "address2")
        city = await self._text(page, "city")
        state = await self._text(page, "state")
        zip_code = await self._text(page, "zip")
        return self._build_address(a1, a2, city, state, zip_code)

    async def _extract_phone(self, page: Page) -> Optional[str]:
        return await self._text(page, "phone")

    async def _extract_fax(self, page: Page) -> Optional[str]:
        return await self._text(page, "fax")

    async def _extract_email(self, page: Page) -> Optional[str]:
        return await self._text(page, "email")

    async def _extract_website(self, page: Page) -> Optional[str]:
        # NC Bar doesn't expose a website field
        return None

    async def _extract_law_school(self, page: Page) -> Optional[str]:
        # NC Bar doesn't publish law school on member search pages
        return None

    async def wait_for_page_load(self, page: Page) -> None:
        """Wait for the detail page to be fully rendered."""
        try:
            await page.wait_for_load_state("networkidle", timeout=20000)
        except Exception:
            pass
        await page.wait_for_timeout(500)

    # ── Private helpers ───────────────────────────────────────────────────────────

    async def _text(self, page: Page, field: str) -> Optional[str]:
        """
        Try each selector for a named field, return stripped text or None.
        """
        for sel in self.SELECTORS.get(field, []):
            try:
                el = await page.query_selector(sel)
                if el:
                    text = (await el.inner_text()).strip()
                    if text and text.lower() not in ("n/a", "not available", "none", "-"):
                        return text
            except Exception:
                continue
        return None

    @staticmethod
    def _parse_body_field(body: str, pattern: str) -> Optional[str]:
        """Regex fallback parser against full visible body text."""
        m = re.search(pattern, body, re.I | re.MULTILINE)
        return m.group(1).strip() if m else None

    @staticmethod
    def _build_address(
        a1: Optional[str],
        a2: Optional[str],
        city: Optional[str],
        state: Optional[str],
        zip_code: Optional[str],
    ) -> Optional[str]:
        """Assemble address components into a single string."""
        parts = [p for p in [a1, a2] if p]
        city_state_zip = ", ".join(filter(None, [city, state]))
        if zip_code:
            city_state_zip = f"{city_state_zip} {zip_code}".strip()
        if city_state_zip:
            parts.append(city_state_zip)
        return ", ".join(parts) if parts else None

    @staticmethod
    def _parse_address_from_body(body: str) -> Optional[str]:
        """Fallback: extract address from body text using common patterns."""
        m = re.search(
            r'Address[:\s]+(.+?)(?:Phone|Fax|Email|County|Status|\n\n)',
            body,
            re.I | re.DOTALL,
        )
        if m:
            addr = re.sub(r'\s+', ' ', m.group(1)).strip().rstrip(",")
            return addr if len(addr) > 5 else None
        return None

    @staticmethod
    def _normalise_nc_name(raw: str) -> str:
        """
        Convert NC Bar "Last, First Middle" format to "First Middle Last".

        Examples:
            "Smith, John"         → "John Smith"
            "Smith, John A"       → "John A Smith"
            "Van Der Berg, Alice" → "Alice Van Der Berg"
            "Jones"               → "Jones"  (no change if no comma)
        """
        if "," not in raw:
            return raw.strip()
        parts = raw.split(",", 1)
        last = parts[0].strip()
        first_middle = parts[1].strip()
        if first_middle:
            return f"{first_middle} {last}"
        return last

    @staticmethod
    def _extract_year(text: Optional[str]) -> Optional[int]:
        """Extract a 4-digit year from an admission date string."""
        if not text:
            return None
        m = re.search(r'\b(19|20)\d{2}\b', text)
        return int(m.group(0)) if m else None
