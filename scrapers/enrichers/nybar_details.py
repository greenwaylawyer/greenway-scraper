"""New York State Bar — Level 2 enricher.

NY Bar data is sourced from the official CSV download (data.ny.gov), which
already contains all available fields (firm, address, phone, law school,
admission year, status).  No Level 2 web enrichment is needed or possible
(the detail portal is behind Cloudflare Bot Management).

This module is kept as a stub so the pipeline import does not break.
"""

from scrapers.enrichers.base_detail import BaseDetailEnricher
from utils.logger import get_logger

logger = get_logger(__name__)


class NYBarDetailEnricher(BaseDetailEnricher):
    """
    Stub enricher for NY Bar.

    The CSV source already provides all available data fields.
    This class is a no-op so the pipeline does not need special-casing.
    """

    SOURCE          = "nybar_details"
    STATE_CODE      = "NY"
    requires_browser_session = False
    skip_level_2    = True  # CSV data is already complete — no enrichment needed

    def construct_detail_url(self, bar_number: str) -> str:
        return (
            f"https://iapps.courts.state.ny.us/attorney/AttorneyDetail"
            f"?registrationNum={bar_number}"
        )

    # ── All abstract methods are no-ops — CSV already contains complete data ──

    async def parse_detail_page(self, page, bar_number: str) -> dict:
        return {}

    async def expand_additional_info(self, page) -> bool:
        return False

    async def _extract_address(self, page):
        return None

    async def _extract_phone(self, page):
        return None

    async def _extract_fax(self, page):
        return None

    async def _extract_email(self, page):
        return None

    async def _extract_website(self, page):
        return None

    async def _extract_law_school(self, page):
        return None
