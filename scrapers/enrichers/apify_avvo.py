"""Apify Avvo enricher (Layer 3).

API-based replacement for the browser-driven ``avvo.py`` enricher. Avvo blocks
headless browsers in production, so we delegate the fetch to the Apify actor
``fatihtahta/avvo-scraper`` and map its dataset records into the fields
``merge_engine.py`` understands.

No browser, no FlareSolverr — just the Apify v2 HTTP API.
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional

from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
from config.loader import get_source_config
from utils.apify_client import ApifyClient, ApifyError
from utils.logger import get_logger

logger = get_logger(__name__)


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

    def _actor_id(self) -> str:
        config = get_source_config(self.get_source_key()) or {}
        apify = config.get("apify", {})
        return apify.get("actor_id") or os.getenv("APIFY_AVVO_ACTOR_ID", "")

    def _input(self, extra: Dict[str, Any]) -> Dict[str, Any]:
        config = get_source_config(self.get_source_key()) or {}
        base = (config.get("apify") or {}).get("input") or {}
        input_data: Dict[str, Any] = {}
        if isinstance(base, dict):
            input_data.update(base)
        input_data.update(extra)
        return input_data

    async def _run_and_collect(self, input_data: Dict[str, Any]) -> List[Dict[str, Any]]:
        actor_id = self._actor_id()
        if not actor_id:
            raise ApifyError("No Apify actor_id configured for apify_avvo")
        config = get_source_config(self.get_source_key()) or {}
        timeout = (config.get("apify") or {}).get("timeout_seconds", 1800)
        run_id = await self.client.run_actor(actor_id, input_data)
        logger.info("Apify Avvo run started", run_id=run_id, actor_id=actor_id)
        status = await self.client.wait_for_finish(run_id, poll_seconds=5, timeout_seconds=timeout)
        if status != "SUCCEEDED":
            raise ApifyError(f"Apify Avvo run {run_id} ended with status {status}")
        items = await self.client.get_dataset_items(run_id)
        logger.info("Apify Avvo run finished", run_id=run_id, items=len(items))
        return items

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
        search_url = self._build_search_url(lawyer)
        try:
            items = await self._run_and_collect(self._input({"startUrls": [search_url], "limit": 10}))
        except ApifyError as exc:
            logger.error("Apify Avvo discovery failed", error=str(exc), query=search_url)
            return []

        candidates: List[DiscoveryCandidate] = []
        for item in items:
            mapped = map_actor_item(item)
            url = mapped.get("source_profile_url")
            name = mapped.get("full_name")
            if not url or not name:
                continue
            address = mapped.get("address") or {}
            location = ", ".join(filter(None, [address.get("city"), address.get("state")]))
            candidate = DiscoveryCandidate(url=url, name=name, location=location)
            self.score_candidate(candidate, lawyer)
            candidates.append(candidate)

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    async def parse_profile(self, url: str, lawyer: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        try:
            items = await self._run_and_collect(self._input({"startUrls": [url], "limit": 10}))
        except ApifyError as exc:
            logger.error("Apify Avvo profile fetch failed", error=str(exc), url=url)
            return None

        profiles = [it for it in items if it.get("type") in (None, "profile")]
        if not profiles:
            return None

        mapped = map_actor_item(profiles[0])
        mapped.setdefault("source_profile_url", url)
        return mapped
