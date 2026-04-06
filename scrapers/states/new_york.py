"""New York State Bar scraper.

Data source
-----------
Official NY Open Data portal (no scraping required):
  https://data.ny.gov/en/Transparency/NYS-Attorney-Registrations/eqw2-r5nb/about_data

Download the CSV and place it at:
  sample-webpage-data/data.ny.gov/NYS_Attorney_Registrations_<date>.csv

The scraper will automatically find the most recent file matching that pattern.

Re-download periodically to keep the data fresh (the dataset is updated monthly).
"""

import csv
import glob
import os
from datetime import datetime
from typing import Optional

from scrapers.base import BaseScraper, LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


# ── Status mapping ─────────────────────────────────────────────────────────────

# All statuses that appear in the NY dataset → canonical license_status value
STATUS_MAP: dict[str, str] = {
    "Currently registered":                    "Active",
    "Due to reregister within 30 days of birthday": "Active",
    "Suspended, currently registered":         "Suspended",
    "Suspended, due to reregister":            "Suspended",
    "Suspended, delinquent":                   "Suspended",
    "Delinquent":                              "Inactive",
    "Resigned":                                "Resigned",
    "Resigned from bar - disciplinary reason": "Resigned",
    "Disbarred":                               "Disbarred",
    "Incapacitated":                           "Inactive",
    "Deceased":                                "Deceased",
}

# Only these statuses are included in the output by default.
# Pass active_only=False (or use --all-statuses flag) to include everything.
ACTIVE_STATUSES: frozenset[str] = frozenset({
    "Currently registered",
    "Due to reregister within 30 days of birthday",
    "Suspended, currently registered",
    "Suspended, due to reregister",
})

# ── CSV column names (as they appear in the downloaded file) ───────────────────
COL_REG_NUM   = "Registration Number"
COL_FIRST     = "First Name"
COL_MIDDLE    = "Middle Name"
COL_LAST      = "Last Name"
COL_SUFFIX    = "Suffix"
COL_COMPANY   = "Company Name"
COL_STREET1   = "Street 1"
COL_STREET2   = "Street 2"
COL_CITY      = "City"
COL_STATE     = "State"
COL_ZIP       = "Zip"
COL_COUNTRY   = "Country"
COL_COUNTY    = "County"
COL_PHONE     = "Phone Number"
COL_YEAR_ADM  = "Year Admitted"
COL_LAW_SCHOOL= "Law School"
COL_STATUS    = "Status"


class StateBarNewYork(BaseScraper):
    """
    Level 1 scraper for NY State Bar attorneys.

    Reads the official CSV download from data.ny.gov — no browser, no Cloudflare.
    ~430,000 total records; ~290,000 active attorneys.

    Level 2 enrichment is not needed: the CSV already contains firm, address,
    phone, law school, admission year, and status for every attorney.
    """

    SOURCE     = "nybar"
    STATE      = "new_york"
    STATE_CODE = "NY"

    # Relative (from project root) data directory
    DATA_DIR = os.path.join("sample-webpage-data", "data.ny.gov")
    CSV_GLOB = "NYS_Attorney_Registrations*.csv"

    def __init__(self, active_only: bool = True, **kwargs):
        super().__init__(**kwargs)
        self.active_only = active_only

    # ── BaseScraper abstract method stubs (not used — CSV source) ──────────────

    def _get_page_url(self, page: int) -> str:
        return ""

    async def get_pagination_info(self, page) -> None:  # type: ignore[override]
        return None

    async def parse_listing(self, page):  # type: ignore[override]
        return
        yield  # make it an async generator

    # ── File discovery ─────────────────────────────────────────────────────────

    def _find_csv(self) -> str:
        """Return the path of the most recent NY Attorney Registrations CSV."""
        project_root = os.path.dirname(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        )
        pattern = os.path.join(project_root, self.DATA_DIR, self.CSV_GLOB)
        matches = sorted(glob.glob(pattern))
        if matches:
            return matches[-1]  # most recent by filename sort

        raise FileNotFoundError(
            "NY Attorney Registrations CSV not found.\n"
            f"Expected pattern: {pattern}\n"
            "Download from:\n"
            "  https://data.ny.gov/en/Transparency/NYS-Attorney-Registrations/eqw2-r5nb/about_data\n"
            f"Save to: {os.path.join(project_root, self.DATA_DIR)}"
        )

    # ── Row parsing ────────────────────────────────────────────────────────────

    @staticmethod
    def _build_name(row: dict) -> tuple[str, Optional[str]]:
        """
        Build (full_name, middle_name) from CSV columns.

        Returns title-cased 'First Middle Last Suffix' and separate middle_name.
        The middle_name is also preserved as a standalone field.
        """
        from normalizers.text import title_name

        first  = title_name(row.get(COL_FIRST,  "").strip()) or ""
        middle = title_name(row.get(COL_MIDDLE, "").strip()) or ""
        last   = title_name(row.get(COL_LAST,   "").strip()) or ""
        suffix = row.get(COL_SUFFIX, "").strip().upper() or ""  # JR, SR etc. stay upper

        parts = [p for p in [first, middle, last, suffix] if p]
        full_name = " ".join(parts)
        return full_name, (middle if middle else None)

    @staticmethod
    def _build_address(row: dict) -> Optional[str]:
        """
        Build a properly formatted address string for Google Maps and USPS parsing.

        Format:  "Street Line, City, ST ZIP"
        Example: "767 3rd Ave Rm 2101, New York, NY 10017"

        Title-cases all components so the address displays correctly.
        """
        from normalizers.text import title_address_line, title_city

        street1  = title_address_line(row.get(COL_STREET1, "").strip()) or ""
        street2  = title_address_line(row.get(COL_STREET2, "").strip()) or ""
        city     = title_city(row.get(COL_CITY,  "").strip()) or ""
        state    = row.get(COL_STATE, "").strip().upper()    # Always uppercase: NY, CA
        zip_code = row.get(COL_ZIP,   "").strip()
        country  = row.get(COL_COUNTRY, "").strip().upper()

        # Build street portion (combine street1 + street2)
        street_parts = [p for p in [street1, street2] if p]
        street = ", ".join(street_parts)

        # Build city-state-zip portion
        # Format: "New York, NY 10017" (NO comma between state and zip — required by AddressNormalizer)
        if city and state and zip_code:
            csz = f"{city}, {state} {zip_code}"
        elif city and state:
            csz = f"{city}, {state}"
        elif city:
            csz = city
        else:
            csz = ""

        # Combine street + city-state-zip
        parts = [p for p in [street, csz] if p]
        if not parts:
            return None

        full = ", ".join(parts)

        # Append country for non-US addresses (helps Google Maps geocoding)
        if country and country != "UNITED STATES OF AMERICA":
            full = f"{full}, {country.title()}"

        return full

    @staticmethod
    def _parse_year(value: str) -> Optional[int]:
        """Parse admission year; return None if missing/invalid."""
        try:
            return int(value.strip())
        except (ValueError, AttributeError):
            return None

    def _row_to_lawyer(self, row: dict) -> Optional[LawyerRawData]:
        """Convert one CSV row to a LawyerRawData object, or None to skip."""
        from normalizers.text import title_firm, title_law_school

        raw_status = row.get(COL_STATUS, "").strip()

        # Status filter
        if self.active_only and raw_status not in ACTIVE_STATUSES:
            return None

        full_name, middle_name = self._build_name(row)
        if not full_name:
            return None  # skip rows with no name

        bar_number     = row.get(COL_REG_NUM,    "").strip() or None
        firm_name      = title_firm(row.get(COL_COMPANY,    "").strip()) or None
        phone          = row.get(COL_PHONE,      "").strip() or None
        law_school     = title_law_school(row.get(COL_LAW_SCHOOL, "").strip()) or None
        address        = self._build_address(row)
        license_status = STATUS_MAP.get(raw_status, raw_status)
        admission_year = self._parse_year(row.get(COL_YEAR_ADM, ""))

        # Judicial Department of Admission: administrative geographic designation,
        # NOT a practice area. Stored as extra context only.
        # 1=Manhattan/Bronx, 2=Brooklyn/LI/Westchester, 3=Albany/Upstate, 4=Buffalo/Rochester
        # (not exposed as a top-level field)

        return LawyerRawData(
            full_name=full_name,
            middle_name=middle_name,
            bar_number=bar_number,
            firm_name=firm_name,
            address=address,
            phone=phone,
            law_school=law_school,
            admission_year=admission_year,
            license_status=license_status,
        )

    # ── Main entry point ───────────────────────────────────────────────────────

    async def run(self) -> list[LawyerRawData]:
        """
        Parse the NY attorney registrations CSV and return LawyerRawData records.

        No browser is launched — this is a pure CSV reader.
        """
        csv_path = self._find_csv()
        logger.info(
            "NY Bar: reading CSV",
            path=csv_path,
            active_only=self.active_only,
        )

        results: list[LawyerRawData] = []
        skipped = 0
        total_rows = 0

        with open(csv_path, newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for row in reader:
                total_rows += 1
                lawyer = self._row_to_lawyer(row)
                if lawyer is None:
                    skipped += 1
                    continue

                results.append(lawyer)

                # Respect --test / --limit flag
                if self.limit and len(results) >= self.limit:
                    logger.info("NY Bar: reached limit, stopping early", limit=self.limit)
                    break

                # Progress log every 50,000 records
                if len(results) % 50_000 == 0:
                    logger.info(
                        "NY Bar: progress",
                        collected=len(results),
                        rows_read=total_rows,
                    )

        logger.info(
            "NY Bar: CSV parse complete",
            total_rows=total_rows,
            collected=len(results),
            skipped=skipped,
            active_only=self.active_only,
            csv_file=os.path.basename(csv_path),
        )

        self.stats["total_collected"] = len(results)
        self.stats["end_time"] = datetime.now().isoformat()

        return results
