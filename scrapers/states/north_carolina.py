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

    # ── Confirmed selectors from live DOM inspection ─────────────────────────────
    # Actual field IDs on portal.ncbar.gov/Verification/search.aspx:
    #   txtFirst, txtMiddle, txtLast, txtCity, txtLicNum
    #   ddState, ddJudicialDistrict, ddLicStatus (values: A=Active, I=Inactive…)
    #   ddLicType, ddSpecialization
    #   btnSubmit (type=submit, value="Search")
    SEL_LAST_NAME   = '#txtLast'
    SEL_FIRST_NAME  = '#txtFirst'
    SEL_STATUS      = '#ddLicStatus'
    SEL_CITY        = '#txtCity'
    SEL_SEARCH_BTN  = '#btnSubmit'

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
            logger.error(
                "Could not find Last Name input (#txtLast) on NC Bar search form",
                url=page.url,
            )
            return

        await last_name_input.fill(last_name_prefix)
        logger.debug(f"Filled Last Name with: {last_name_prefix!r}")

        # ── Member Status = Active (value='A') ──────────────────────────────────
        status_sel = await page.query_selector(self.SEL_STATUS)
        if status_sel:
            try:
                await status_sel.select_option(value="A")
                logger.debug("Set Status to Active (value='A')")
            except Exception as e:
                logger.warning(f"Could not set Status dropdown: {e}")
        else:
            logger.warning("Member Status dropdown (#ddLicStatus) not found")

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
            # Fallback: any submit button
            fallback = await page.query_selector('input[type="submit"]')
            if fallback:
                await fallback.click()
            else:
                await last_name_input.press("Enter")

        await page.wait_for_load_state("networkidle", timeout=30000)
        await page.wait_for_timeout(800)

    async def _count_results(self, page: Page) -> int:
        """
        Count results by reading the results table row count.

        NC Bar returns a single <table class="table table-hover"> with
        1 header row + up to 250 data rows.
        """
        soup = BeautifulSoup(await page.content(), "html.parser")
        table = self._find_results_table(soup)
        if not table:
            return 0
        data_rows = [r for r in table.find_all("tr") if r.find_all("td")]
        return len(data_rows)

    # ── Core parsing ─────────────────────────────────────────────────────────────

    @staticmethod
    def _find_results_table(soup: BeautifulSoup):
        """
        Locate the results table.

        The NC Bar portal renders a single Bootstrap table:
            <table class="table table-hover"> … </table>
        with columns: Bar ID | Name | Type | Status | Location | Judicial District
        """
        # Primary: Bootstrap table-hover (confirmed from live DOM)
        t = soup.find("table", class_=lambda c: c and "table-hover" in c)
        if t:
            return t

        # Fallback: any table with a "Bar ID" or "Name" column header
        for table in soup.find_all("table"):
            headers = [th.get_text(strip=True).lower() for th in table.find_all("th")]
            if any(h in ("bar id", "name", "status") for h in headers):
                return table

        # Last resort: largest table with data rows
        tables = soup.find_all("table")
        data_tables = [t for t in tables if len(t.find_all("tr")) > 2 and t.find("td")]
        if data_tables:
            return max(data_tables, key=lambda t: len(t.find_all("tr")))

        return None

    async def parse_listing(self, page: Page) -> AsyncIterator[LawyerRawData]:  # type: ignore[override]
        """
        Parse the search results table for the current search.

        Confirmed NC Bar column order (table.table-hover):
            Col 0: Bar ID          (plain number — this is the bar/license number)
            Col 1: Name            (<a href="/Verification/viewer.aspx?ID=XXXXX">)
            Col 2: Type            (Attorney, Judge, Corporation…)
            Col 3: Status          (<span class="label label-success">Active</span>)
            Col 4: Location        (City, ST)
            Col 5: Judicial District

        Note: the internal URL ID (`viewer.aspx?ID=...`) can differ from the
        Bar ID — we always take the bar number from col 0 and the detail URL
        from the href in col 1.
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

                # ── Col 0: Bar ID (license/bar number) ──────────────────────────
                bar_number: Optional[str] = cols[0].get_text(strip=True) or None

                # ── Col 1: Name + detail URL ─────────────────────────────────────
                link = cols[1].find("a", href=True)
                detail_url: Optional[str] = None
                if link:
                    href = link["href"]
                    detail_url = (
                        href if href.startswith("http")
                        else f"{self.BASE_URL}{href}"
                    )
                raw_name = cols[1].get_text(" ", strip=True)
                full_name = self._clean_name(raw_name)

                if not full_name:
                    continue

                # ── Col 3: Status (may be wrapped in <span>) ─────────────────────
                license_status: Optional[str] = None
                if len(cols) > 3:
                    license_status = cols[3].get_text(strip=True) or None

                # ── Col 4: Location (city only at Level 1) ───────────────────────
                # We intentionally skip storing city-as-address; full address
                # comes from Level 2 detail page scrape.

                logger.debug(
                    f"Parsed: name={full_name!r}  bar={bar_number!r}  "
                    f"status={license_status!r}  url={detail_url!r}"
                )

                yield LawyerRawData(
                    full_name=full_name,
                    bar_number=bar_number,
                    license_status=license_status,
                    address=None,       # Full address comes from Level 2
                    detail_url=detail_url,
                    raw_html=str(row),
                )

            except Exception as exc:
                logger.error("NC Bar: failed to parse result row", error=str(exc))
                continue

    @staticmethod
    def _clean_name(raw: str) -> str:
        """
        Strip honorifics and extra whitespace from a name string.

        Examples:
            "Ms. Nana Asante-Smith"  → "Nana Asante-Smith"
            "Mr. Peter F. Asmer, Jr." → "Peter F. Asmer, Jr."
            "Judge Monica M. Bousman" → "Monica M. Bousman"
        """
        # Remove leading honorifics (Mr., Mrs., Ms., Dr., Judge, Hon., etc.)
        cleaned = re.sub(
            r'^(?:Mr\.|Mrs\.|Ms\.|Dr\.|Prof\.|Judge|Hon\.|Sir|Lady)\s+',
            '',
            raw.strip(),
            flags=re.I,
        )
        return cleaned.strip()

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
                    # Check limit
                    if self.limit and len(all_lawyers) >= self.limit:
                        logger.info(f"Limit reached ({self.limit} lawyers), stopping scrape")
                        break

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
                                # Check limit
                                if self.limit and len(all_lawyers) >= self.limit:
                                    break

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
