"""North Carolina State Bar scraper.

NC Bar portal: https://portal.ncbar.gov/Verification/search.aspx

Search strategy
-----------
The portal enforces query constraints: valid searches must include
one of the following:

  - State Bar ID  OR
  - First 2 chars of Last Name  OR
  - Board Certified Specialist  OR
  - Status AND first 3 chars of City  OR
  - Judicial District AND first 3 chars of City

We use the most comprehensive path: **2-char last name prefix + Status=Active**.
This gives 676 searches (aa→zz) that collectively cover every active NC lawyer
without any city/district bias.

Overflow protection
-------------------
The portal caps results at 250.  If a 2-char prefix returns >= OVERFLOW_THRESHOLD
results we expand to 3-char sub-sweeps (aaa→zzz) for that prefix so no records
are missed.

Deduplication
-------------
Bar numbers are tracked in a seen-set so lawyers who appear in multiple prefix
buckets (edge-case) are not double-counted.
"""

import itertools
import re
import string
from typing import AsyncIterator, List, Optional

from bs4 import BeautifulSoup
from playwright.async_api import Page

from scrapers.base import BaseScraper, LawyerRawData, PaginationInfo
from utils.logger import get_logger

logger = get_logger(__name__)

RESULT_LIMIT = 250
# If a search returns this many results we treat it as potentially truncated
OVERFLOW_THRESHOLD = 245


class StateBarNorthCarolina(BaseScraper):
    """
    Scraper for the North Carolina State Bar member directory.

    Level 1: Alphabet sweep over 2-char last-name prefixes with Status=Active.
             Automatically widens to 3-char sweep for any bucket that hits the
             250-result cap.
    Level 2: See ncbar_details.py – handled by NorthCarolinaDetailEnricher.
    """

    BASE_URL = "https://portal.ncbar.gov"
    STATE_CODE = "north_carolina"
    RATE_LIMIT = 15          # conservatively low to avoid throttling
    SOURCE = "ncbar"
    SEARCH_URL = "https://portal.ncbar.gov/Verification/search.aspx"

    # ── Selectors (ASP.NET WebForms with generated IDs ending in field name) ─────
    # We use partial-match attribute selectors ($=) so they survive master-page
    # content placeholder prefixes like "ctl00$ContentPlaceHolder1$..."
    SEL_LAST_NAME   = 'input[id$="txtLastName"], input[id$="LastName"], input[name$="txtLastName"]'
    SEL_FIRST_NAME  = 'input[id$="txtFirstName"], input[id$="FirstName"]'
    SEL_STATUS      = 'select[id$="ddlMemberStatus"], select[id$="MemberStatus"], select[name$="ddlMemberStatus"]'
    SEL_CITY        = 'input[id$="txtCity"], input[id$="City"], input[name$="txtCity"]'
    SEL_SEARCH_BTN  = 'input[type="submit"][id$="btnSearch"], input[type="submit"][value="Search"], input[type="submit"]'

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._seen_bar_numbers: set[str] = set()

    # ── Internal helpers ─────────────────────────────────────────────────────────

    def _two_char_prefixes(self) -> List[str]:
        """Return all 676 lowercase 2-char combos (aa → zz)."""
        return [a + b for a, b in itertools.product(string.ascii_lowercase, repeat=2)]

    def _three_char_prefixes(self, base: str) -> List[str]:
        """Return all 26 extensions for a given 2-char base (e.g. sm → sma…smz)."""
        return [base + c for c in string.ascii_lowercase]

    async def _navigate_to_form(self, page: Page) -> None:
        """Navigate to a fresh search form, discarding any previous results."""
        await page.goto(self.SEARCH_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_load_state("domcontentloaded")
        # Brief pause to ensure all ASP.NET __VIEWSTATE tokens are initialised
        await page.wait_for_timeout(500)

    async def _fill_and_submit(
        self,
        page: Page,
        last_name_prefix: str,
        city_prefix: Optional[str] = None,
    ) -> None:
        """
        Fill the search form and submit it.

        Navigates to a fresh form first so VIEWSTATE is always valid.
        """
        await self._navigate_to_form(page)

        # ── Last name prefix ────────────────────────────────────────────────────
        last_name_input = await page.query_selector(self.SEL_LAST_NAME)
        if not last_name_input:
            # Fall back: find the input closest to a "Last Name" label
            last_name_input = await page.query_selector('input[type="text"]:nth-of-type(3)')
        if last_name_input:
            await last_name_input.fill(last_name_prefix)
        else:
            logger.error("Could not find Last Name input on NC Bar search form")
            return

        # ── Member Status = Active ──────────────────────────────────────────────
        status_sel = await page.query_selector(self.SEL_STATUS)
        if status_sel:
            try:
                await status_sel.select_option(label="Active")
            except Exception:
                try:
                    await status_sel.select_option(value="Active")
                except Exception:
                    # Try by index – "Active" is usually option 1 or 2
                    options = await status_sel.query_selector_all("option")
                    for opt in options:
                        text = (await opt.text_content() or "").strip()
                        if "active" in text.lower():
                            val = await opt.get_attribute("value")
                            if val:
                                await status_sel.select_option(value=val)
                            break
        else:
            logger.warning("Could not locate Member Status dropdown; proceeding without it")

        # ── Optional city filter ────────────────────────────────────────────────
        if city_prefix:
            city_input = await page.query_selector(self.SEL_CITY)
            if city_input:
                await city_input.fill(city_prefix)

        # ── Submit ──────────────────────────────────────────────────────────────
        search_btn = await page.query_selector(self.SEL_SEARCH_BTN)
        if search_btn:
            await search_btn.click()
        else:
            # ASP.NET fallback: press Enter in the last-name field
            await last_name_input.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(800)

    async def _count_results(self, page: Page) -> int:
        """
        Extract the number of results from the current search result page.
        Returns 0 when no results section is visible.
        """
        content = await page.content()

        # Pattern 1: "250 Records Found" / "12 Results Found"
        match = re.search(r'(\d+)\s+(?:Records?|Results?|Attorneys?|Members?)\s+(?:Found|Returned|Listed)', content, re.I)
        if match:
            return int(match.group(1))

        # Pattern 2: count table rows directly
        rows = await page.query_selector_all(
            'table tr.GridRow, table tr.GridAltRow, '
            'table tr[class*="row"], table tr[class*="odd"], table tr[class*="even"], '
            'table.searchResults tr:not(:first-child)'
        )
        if rows:
            return len(rows)

        # Pattern 3: any data-bearing table (heuristic)
        soup = BeautifulSoup(content, "html.parser")
        table = self._find_results_table(soup)
        if table:
            data_rows = [r for r in table.find_all("tr") if r.find_all("td")]
            return len(data_rows)

        return 0

    # ── Core parsing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _find_results_table(soup: BeautifulSoup):
        """
        Locate the search results table in the page soup.

        Tries multiple heuristics in priority order so the scraper is
        resilient to minor HTML changes on the portal.
        """
        # 1. Table with a GridView id
        t = soup.find("table", id=re.compile(r"GridView|gvResults|Grid|Results", re.I))
        if t:
            return t

        # 2. Table by class
        for cls_hint in ("grid", "searchresult", "result", "members"):
            t = soup.find("table", class_=re.compile(cls_hint, re.I))
            if t:
                return t

        # 3. Table that contains a MemberDetail link — strongest signal
        for table in soup.find_all("table"):
            if table.find("a", href=re.compile(r"MemberDetail|member|detail|profile|id=", re.I)):
                return table

        # 4. Largest table on the page (last resort)
        tables = soup.find_all("table")
        if tables:
            # Filter out navigation / layout tables (those with few columns)
            data_tables = [t for t in tables if len(t.find_all("tr")) > 2 and t.find("td")]
            if data_tables:
                return max(data_tables, key=lambda t: len(t.find_all("tr")))

        return None

    async def parse_listing(self, page: Page) -> AsyncIterator[LawyerRawData]:  # type: ignore[override]
        """
        Parse the search results table for the current search.

        NC Bar result columns (typical order):
            Name | State Bar ID | Status | City | County
        """
        html = await page.content()
        soup = BeautifulSoup(html, "html.parser")
        table = self._find_results_table(soup)

        if not table:
            logger.warning("NC Bar: no results table found on page", url=page.url)
            return

        rows = table.find_all("tr")
        data_rows = [r for r in rows if r.find_all("td")]
        logger.info(f"NC Bar: found {len(data_rows)} result rows")

        for row in data_rows:
            try:
                cols = row.find_all("td")
                if len(cols) < 2:
                    continue

                # ── Detail URL ──────────────────────────────────────────────────
                link = row.find("a", href=True)
                detail_url: Optional[str] = None
                if link:
                    href = link["href"]
                    if href.startswith("http"):
                        detail_url = href
                    elif href.startswith("/"):
                        detail_url = f"{self.BASE_URL}{href}"
                    else:
                        detail_url = f"{self.BASE_URL}/Verification/{href}"

                # ── Name ────────────────────────────────────────────────────────
                # Name is always in the first column; strip trailing bar-ID text
                full_name = cols[0].get_text(" ", strip=True)
                # Some portals embed the bar number in the name cell: "Smith, John (12345)"
                full_name = re.sub(r'\s*\(\d+\)\s*$', '', full_name).strip()

                if not full_name:
                    continue

                # ── Bar Number ──────────────────────────────────────────────────
                # Try column 1 first; if it looks like a number, use it
                bar_number: Optional[str] = None
                if len(cols) > 1:
                    candidate = cols[1].get_text(strip=True)
                    if re.match(r'^\d+$', candidate):
                        bar_number = candidate
                    else:
                        # Try reading bar number from the detail URL ?id=XXXXX
                        if detail_url:
                            m = re.search(r'[?&](?:id|memberid|barid)=(\d+)', detail_url, re.I)
                            if m:
                                bar_number = m.group(1)

                # ── License Status ──────────────────────────────────────────────
                license_status: Optional[str] = None
                if len(cols) > 2:
                    license_status = cols[2].get_text(strip=True) or None

                # ── City ────────────────────────────────────────────────────────
                # City alone is not a full address; Level 2 will retrieve full address
                # (we still capture it for quick filtering)
                city: Optional[str] = None
                if len(cols) > 3:
                    city = cols[3].get_text(strip=True) or None

                yield LawyerRawData(
                    full_name=full_name,
                    bar_number=bar_number,
                    license_status=license_status,
                    address=None,       # Full address scraped in Level 2
                    detail_url=detail_url,
                    raw_html=str(row),
                )

            except Exception as exc:
                logger.error("NC Bar: failed to parse result row", error=str(exc))
                continue

    async def get_pagination_info(self, page: Page) -> PaginationInfo:
        """
        NC Bar caps results at 250 per search – no multi-page pagination.
        Our prefix-sweep strategy ensures we never exceed the cap per query.
        """
        return PaginationInfo(current_page=1, has_next=False)

    def _get_page_url(self, page: int) -> str:
        return self.SEARCH_URL

    # ── Main run loop ─────────────────────────────────────────────────────────────

    async def run(self) -> list[LawyerRawData]:
        """
        Run the full Level 1 scrape using a 2-char last-name prefix sweep.

        Algorithm
        ---------
        for prefix in aa…zz:
            search(prefix, status=Active)
            if results >= OVERFLOW_THRESHOLD:
                for sub in prefix+'a'…prefix+'z':
                    search(sub, status=Active)  ← 3-char sweep
            else:
                collect results
        deduplicate by bar_number
        save checkpoint after each prefix
        """
        from datetime import datetime
        from playwright.async_api import async_playwright

        self.stats["start_time"] = datetime.now().isoformat()
        all_lawyers: list[LawyerRawData] = []
        self._seen_bar_numbers = set()

        logger.info("Starting NC Bar Level 1 scrape (2-char prefix sweep)…")

        async with async_playwright() as playwright:
            self.browser = await playwright.chromium.launch(headless=self.headless)
            self.context = await self.browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                )
            )
            self.page = await self.context.new_page()

            try:
                prefixes = self._two_char_prefixes()   # ['aa','ab',…,'zz']
                resume_from: Optional[str] = None

                # ── Checkpoint resume ───────────────────────────────────────────
                if self.checkpoint_enabled and self.checkpoint:
                    ckpt = self.checkpoint.load()
                    if ckpt and "stats" in ckpt:
                        resume_from = ckpt["stats"].get("last_prefix")
                        self._seen_bar_numbers = set(ckpt["stats"].get("seen_bar_numbers", []))
                        # Reconstruct previously collected lawyers
                        for raw in ckpt.get("lawyers", []):
                            try:
                                all_lawyers.append(LawyerRawData(**{
                                    k: v for k, v in raw.items()
                                    if k in LawyerRawData.__dataclass_fields__
                                }))
                            except Exception:
                                pass
                        logger.info(
                            f"Resuming NC scrape after prefix='{resume_from}', "
                            f"{len(all_lawyers)} lawyers already collected"
                        )

                # ── Main sweep ──────────────────────────────────────────────────
                for prefix in prefixes:
                    if resume_from and prefix <= resume_from:
                        continue

                    await self.rate_limiter.acquire()

                    try:
                        # Submit the form for this prefix
                        await self._fill_and_submit(self.page, prefix)
                        count = await self._count_results(self.page)

                        if count >= OVERFLOW_THRESHOLD:
                            # ── 3-char sub-sweep for this bucket ───────────────
                            logger.info(
                                f"Prefix '{prefix}' returned {count} results (>= {OVERFLOW_THRESHOLD}) "
                                "– expanding to 3-char sub-sweep"
                            )
                            for sub_prefix in self._three_char_prefixes(prefix):
                                await self.rate_limiter.acquire()
                                try:
                                    await self._fill_and_submit(self.page, sub_prefix)
                                    async for lawyer in self.parse_listing(self.page):
                                        self._add_unique(all_lawyers, lawyer)
                                except Exception as sub_exc:
                                    logger.error(
                                        f"Sub-prefix '{sub_prefix}' failed: {sub_exc}"
                                    )
                        else:
                            # ── Normal: collect results for this prefix ─────────
                            async for lawyer in self.parse_listing(self.page):
                                self._add_unique(all_lawyers, lawyer)

                    except Exception as prefix_exc:
                        logger.error(f"Prefix '{prefix}' failed: {prefix_exc}")
                        self.stats["errors"] = self.stats.get("errors", 0) + 1

                    # ── Update stats ────────────────────────────────────────────
                    self.stats["lawyers_found"] = len(all_lawyers)

                    # ── Save checkpoint ─────────────────────────────────────────
                    if self.checkpoint_enabled and self.checkpoint:
                        self.checkpoint.save({
                            "lawyers": [
                                {f: getattr(lw, f) for f in LawyerRawData.__dataclass_fields__}
                                for lw in all_lawyers
                            ],
                            "stats": {
                                "last_prefix": prefix,
                                "seen_bar_numbers": list(self._seen_bar_numbers),
                                **self.stats,
                            },
                        })

                    # ── Live batch-tracker update ───────────────────────────────
                    if self.batch_tracker:
                        self.batch_tracker.update_progress(
                            records_processed=len(all_lawyers),
                            metadata={
                                "last_prefix": prefix,
                                "total_lawyers": len(all_lawyers),
                            },
                        )

                    logger.info(
                        f"NC Bar prefix '{prefix}' done — running total: {len(all_lawyers)}"
                    )

            except Exception as exc:
                logger.error(f"NC Bar scrape failed: {exc}")
                raise
            finally:
                await self.browser.close()

        self.stats["end_time"] = datetime.now().isoformat()
        logger.info(
            f"NC Bar Level 1 complete — {len(all_lawyers)} unique active lawyers collected"
        )
        return all_lawyers

    # ── Helper ───────────────────────────────────────────────────────────────────

    def _add_unique(self, all_lawyers: list, lawyer: LawyerRawData) -> None:
        """Add to list only if bar_number not already seen (or no bar number)."""
        if lawyer.bar_number:
            if lawyer.bar_number in self._seen_bar_numbers:
                return
            self._seen_bar_numbers.add(lawyer.bar_number)
        all_lawyers.append(lawyer)
