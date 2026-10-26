"""Apify Avvo enricher (Layer 3).

API-based replacement for the browser-driven ``avvo.py`` enricher. Avvo blocks
headless browsers in production, so pages are fetched with the generic
``apify/web-fetch`` actor and parsed with ``avvo.py``'s HTML parsers
(search cards and profile JSON-LD + sections, including the headshot).

The dedicated ``fatihtahta/avvo-scraper`` actor got blocked in Oct 2026 (runs
looped on failed fetches until the 1h timeout). ``map_actor_item`` is kept for
reading datasets that actor produced earlier.

No browser, no FlareSolverr — just the Apify v2 HTTP API.
"""

from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from scrapers.enrichers.avvo import AvvoEnricher
from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
from utils.apify_client import ApifyClient, ApifyError
from utils.logger import get_logger

logger = get_logger(__name__)

_AVVO_ID_RE = re.compile(r"-(\d+)\.html")


def avvo_id_from_url(url: Optional[str]) -> Optional[str]:
    """Avvo's numeric lawyer ID is the trailing number of a profile URL."""
    match = _AVVO_ID_RE.search(url or "")
    return match.group(1) if match else None


def _first(data: Dict[str, Any], *keys: str) -> Any:
    for key in keys:
        value = data.get(key)
        if value not in (None, "", [], {}):
            return value
    return None


def _as_list(value: Any) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _str(value: Any) -> str:
    if value is None:
        return ""
    return re.sub(r"\s+", " ", str(value)).strip()


def map_actor_item(item: Dict[str, Any]) -> Dict[str, Any]:
    """Map one ``fatihtahta/avvo-scraper`` profile record into merged_data fields.

    Reference (type = "profile"): name, jobTitle, address, city, state,
    primary_phone, specialties[], practiceAreas[{name, share}], website,
    linkedin, rating, reviewCount, endorsementsReceived, education[],
    awards[], bioSummary, yearsActiveEstimate.
    """
    full_name = _str(_first(item, "name", "full_name")) or None

    bio = _str(_first(item, "bioSummary", "jobTitle", "bio", "about")) or None
    tagline = _str(_first(item, "jobTitle")) or None

    specialties = [_str(s) for s in _as_list(_first(item, "specialties"))]
    practice_areas: List[str] = []
    for entry in _as_list(_first(item, "practiceAreas", "practice_areas")):
        if isinstance(entry, dict):
            entry = _first(entry, "name", "title", "practiceArea")
        text = _str(entry)
        if text and text.lower() not in {p.lower() for p in practice_areas}:
            practice_areas.append(text)
    for area in specialties:
        if area and area.lower() not in {p.lower() for p in practice_areas}:
            practice_areas.append(area)

    phone = _str(_first(item, "primary_phone", "phone", "phoneNumber")) or None
    if phone:
        phone = BaseEnricher.normalize_phone(phone) or phone

    address = _normalize_address(item)

    rating = _first(item, "rating")
    review_count = _first(item, "reviewCount", "reviewsCount", "review_count")
    endorsements = _first(item, "endorsementsReceived")

    license_state = _str(_first(item, "state", "licenseState")) or None

    education = _as_list(_first(item, "education", "educationHistory", "education_history"))
    awards = _as_list(_first(item, "awards"))

    website = _str(_first(item, "website", "websiteUrl")) or None
    linkedin = _str(_first(item, "linkedin")) or None

    social_links: Dict[str, str] = {}
    if linkedin:
        social_links["linkedin"] = linkedin

    websites = [website] if website else []

    years_active = _first(item, "yearsActiveEstimate", "years_experience")

    result: Dict[str, Any] = {
        "full_name": full_name,
        "bio": bio,
        "tagline": tagline,
        "practice_areas": practice_areas,
        "education_history": education,
        "awards": awards,
        "photo_url": None,
        "phone": phone,
        "address": address,
        "avvo_rating": rating,
        "client_review_count": review_count,
        "endorsements_received": endorsements,
        "years_experience": years_active,
        "languages": [],
        "websites": websites,
        "social_links": social_links or None,
        "bar_number": None,
        "license_state": license_state,
        "source_profile_url": _str(_first(item, "url")) or None,
        "avvo_id": _first(item, "id"),
    }

    return {
        k: v
        for k, v in result.items()
        if v is not None and v != "" and not (isinstance(v, (list, dict)) and len(v) == 0)
    }


def _normalize_address(item: Dict[str, Any]) -> Optional[Dict[str, str]]:
    city = _str(_first(item, "city"))
    state = _str(_first(item, "state"))
    address_text = _str(_first(item, "address"))

    if not city and not state and not address_text:
        return None

    line1 = address_text
    if city and line1 and city in line1:
        line1 = line1.split(city)[0].rstrip(", ")

    result = {"line1": line1, "city": city, "state": state}
    return {k: v for k, v in result.items() if v}


class ApifyAvvoEnricher(BaseEnricher):
    """Avvo enrichment through the Apify Avvo scraper actor."""

    def __init__(self):
        self.client = ApifyClient()

    def get_source_key(self) -> str:
        return "avvo"

    def _build_search_url(self, lawyer: Dict[str, Any]) -> str:
        from urllib.parse import urlencode

        full_name = lawyer.get("full_name") or " ".join(
            filter(None, [lawyer.get("first_name"), lawyer.get("last_name")])
        )
        city = (lawyer.get("city") or "").strip()
        state = (lawyer.get("license_state") or lawyer.get("state") or "").strip()
        location = ", ".join(filter(None, [city, state]))
        return "https://www.avvo.com/search/lawyer_search?" + urlencode(
            {"q": full_name, "loc": location}
        )

    async def discover_profile(self, lawyer: Dict[str, Any]) -> List[DiscoveryCandidate]:
        # Avvo search page via apify/web-fetch, parsed with the browser enricher's
        # card parser. The Avvo actor got blocked (runs looped until the 1h timeout).
        search_url = self._build_search_url(lawyer)
        try:
            html = await self.client.fetch_html(search_url)
        except ApifyError as exc:
            logger.error("Avvo search fetch failed", error=str(exc), query=search_url)
            return []
        if not html:
            logger.warning("Avvo search fetch returned no page", query=search_url)
            return []

        candidates = AvvoEnricher._parse_search_cards(html)
        for candidate in candidates:
            self.score_candidate(candidate, lawyer)
        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    async def parse_profile(self, url: str, lawyer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            html = await self.client.fetch_html(url)
        except ApifyError as exc:
            logger.error("Avvo profile fetch failed", error=str(exc), url=url)
            return None
        if not html:
            return None

        parsed = AvvoEnricher.parse_profile_html(html, url)
        if parsed:
            parsed.setdefault("avvo_id", avvo_id_from_url(url))
        return parsed
