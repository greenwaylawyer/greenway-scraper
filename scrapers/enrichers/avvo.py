"""
Avvo.com enricher for Layer 3.

HTML structure confirmed by inspection (April 2026):
  Search: .serp-card.organic-card — contains JSON-LD (type=Person) per card
  Profile: JSON-LD (LocalBusiness) + HTML sections

Key selectors on the profile page:
  bio         .about-container .show-less-bio p
  practice    JSON-LD knowsAbout  /  .practice-area-title strong
  rating      .rating-row .review-score span (client)
              .avvo-ratings .avvo-rating-count span (Avvo)
  education   .education-container .experience  (<p>=year, <strong>=school, <p>=degree)
  work        .work-experience-container .experience
  assoc       .associations-container .experience
  languages   .languages-container .languages-list p
  publications.publications-container .experience
  photo       JSON-LD image.url  /  .headshot img
  social      JSON-LD sameAs
"""

import asyncio
import os
import random
import re
from typing import Dict, Any, List, Optional
from urllib.parse import urlencode, urljoin
import json

from bs4 import BeautifulSoup

from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
from utils.logger import get_logger

logger = get_logger(__name__)

FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "")


async def _fetch_via_flaresolverr(url: str, timeout_ms: int = 60_000) -> str:
    import aiohttp
    payload  = {"cmd": "request.get", "url": url, "maxTimeout": timeout_ms}
    endpoint = FLARESOLVERR_URL.rstrip("/") + "/v1"
    async with aiohttp.ClientSession() as session:
        async with session.post(
            endpoint, json=payload,
            timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000 + 10),
        ) as resp:
            data = await resp.json()
    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message', data)}")
    return data["solution"]["response"]


class AvvoEnricher(BaseEnricher):
    """Avvo.com enrichment source for Layer 3."""

    BASE_URL = "https://www.avvo.com"

    _USER_AGENTS = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    ]

    _playwright = None
    _browser    = None

    def get_source_key(self) -> str:
        return "avvo"

    # ── HTTP fetch (FlareSolverr → Playwright fallback) ───────────────────────

    async def _get_browser(self):
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-blink-features=AutomationControlled"],
            )
        return self._browser

    async def close(self) -> None:
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _fetch_html(self, url: str) -> str:
        if FLARESOLVERR_URL:
            try:
                html = await _fetch_via_flaresolverr(url)
                logger.info("FlareSolverr fetch OK", url=url, length=len(html))
                return html
            except Exception as e:
                logger.warning("FlareSolverr failed, falling back to Playwright",
                               url=url, error=str(e))

        # Playwright fallback
        try:
            from playwright_stealth import Stealth
            _stealth = Stealth(navigator_webdriver=True)
        except ImportError:
            _stealth = None

        browser = await self._get_browser()
        ua      = random.choice(self._USER_AGENTS)
        context = await browser.new_context(
            user_agent=ua, viewport={"width": 1440, "height": 900},
            locale="en-US",
        )
        page = await context.new_page()
        if _stealth:
            await _stealth.apply_stealth_async(page)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)
            for _ in range(10):
                if "just a moment" not in (await page.title()).lower():
                    break
                await asyncio.sleep(1)
            await asyncio.sleep(random.uniform(0.6, 1.2))
            html = await page.content()
        finally:
            await context.close()
        return html

    # ── JSON-LD helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _extract_jsonld(soup: BeautifulSoup, type_filter: Optional[str] = None) -> Dict[str, Any]:
        """Return the first JSON-LD object (optionally filtered by @type)."""
        for script in soup.select('script[type="application/ld+json"]'):
            try:
                d = json.loads(script.string or "")
            except Exception:
                continue
            if type_filter is None or d.get("@type") == type_filter:
                return d
        return {}

    # ── Search result parsing ─────────────────────────────────────────────────

    @staticmethod
    def _clean(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip()

    @staticmethod
    def _parse_search_cards(html: str) -> List[DiscoveryCandidate]:
        """
        Extract DiscoveryCandidate objects from Avvo search results.

        Each card (.serp-card.organic-card) embeds a JSON-LD <script> of
        type Person that contains the profile URL, name, photo, and address.
        """
        soup = BeautifulSoup(html, "html.parser")
        cards = soup.select(".serp-card.organic-card")

        # url → best candidate (keep longest name)
        url_to_best: Dict[str, DiscoveryCandidate] = {}

        for card in cards:
            # 1. Get profile URL (first /attorneys/ link without #)
            profile_url = None
            for a in card.select('a[href*="/attorneys/"]'):
                href = a.get("href", "")
                if "#" in href or not href:
                    continue
                profile_url = href if href.startswith("http") else "https://www.avvo.com" + href
                break
            if not profile_url:
                continue

            # 2. Try JSON-LD inside the card for structured data
            name     = ""
            location = ""
            phone    = ""
            photo    = ""

            ld_script = card.select_one('script[type="application/ld+json"]')
            if ld_script:
                try:
                    ld = json.loads(ld_script.string or "")
                    name     = AvvoEnricher._clean(ld.get("name", ""))
                    wa       = ld.get("worksFor", ld)
                    addr     = wa.get("address", {})
                    city     = addr.get("addressLocality", "")
                    state    = addr.get("addressRegion", "")
                    location = ", ".join(filter(None, [city, state]))
                    phone    = wa.get("telephone", "")
                    img_obj  = ld.get("image", {})
                    photo    = img_obj.get("url", "") if isinstance(img_obj, dict) else str(img_obj)
                except Exception:
                    pass

            # 3. Fallback: extract name from visible link text
            if not name:
                for a in card.select('a[href*="/attorneys/"]'):
                    t = AvvoEnricher._clean(a.get_text(" ", strip=True))
                    skip = {"view profile", "view lawyerprofile", "contact", "reviews"}
                    if t and len(t) >= 3 and t.lower() not in skip:
                        name = t
                        break

            if not name:
                continue

            existing = url_to_best.get(profile_url)
            if existing is None or len(name) > len(existing.name):
                c = DiscoveryCandidate(url=profile_url, name=name, location=location)
                c.phone    = phone
                c.photo    = photo
                url_to_best[profile_url] = c

        return list(url_to_best.values())

    # ── Discovery ─────────────────────────────────────────────────────────────

    def _build_search_url(self, name: str, location: str) -> str:
        params = {"utf8": "✓", "q": name, "loc": location, "commit": ""}
        return f"{self.BASE_URL}/search/lawyer_search?{urlencode(params)}"

    async def discover_profile(
        self,
        lawyer: Dict[str, Any],
    ) -> List[DiscoveryCandidate]:
        """Search Avvo for matching lawyer profile."""
        full_name   = lawyer.get("full_name") or ""
        city        = (lawyer.get("city") or "").strip()
        state_code  = (lawyer.get("license_state") or lawyer.get("state") or "").strip()

        # Try several location filters for robustness
        locations: List[str] = []
        if city and state_code:
            locations.append(f"{city} {state_code}")
        if city:
            locations.append(city)
        if state_code:
            locations.append(state_code)
        locations.append("")   # fallback: no location filter

        candidates_map: Dict[str, DiscoveryCandidate] = {}

        for loc in locations:
            search_url = self._build_search_url(full_name, loc)
            logger.info("Avvo discovery search",
                        url=search_url, lawyer=full_name, location=loc or "(none)")
            try:
                html = await self._fetch_html(search_url)
            except Exception as e:
                logger.error("Avvo search fetch failed", url=search_url, error=str(e))
                continue

            for c in self._parse_search_cards(html):
                if c.url not in candidates_map:
                    candidates_map[c.url] = c

            # Stop early if we already have a name-exact match
            exact = [c for c in candidates_map.values()
                     if c.name.lower() == full_name.lower()]
            if exact:
                break

            await asyncio.sleep(random.uniform(0.8, 1.5))

        candidates = list(candidates_map.values())

        for c in candidates:
            self.score_candidate(c, lawyer)

        candidates.sort(key=lambda c: c.confidence, reverse=True)

        logger.info(
            "Avvo discovery completed",
            total=len(candidates),
            top_confidence=candidates[0].confidence if candidates else 0,
            top_url=candidates[0].url if candidates else None,
        )

        try:
            await self.close()
        except Exception:
            pass

        return candidates

    # ── Profile parsing ───────────────────────────────────────────────────────

    @staticmethod
    def _parse_experience_section(section) -> List[Dict[str, Any]]:
        """Parse .experience divs inside a section container (associations, publications)."""
        results = []
        for exp in section.select(".experience"):
            entry: Dict[str, Any] = {}

            year_p = exp.find("p")
            if year_p:
                entry["years"] = AvvoEnricher._clean(year_p.get_text())

            strong = exp.find("strong")
            if strong:
                entry["title"] = AvvoEnricher._clean(strong.get_text()).rstrip(",").strip()

            # Remaining text after removing year + title
            all_text = AvvoEnricher._clean(exp.get_text(" "))
            remainder = all_text
            for part in [entry.get("years", ""), entry.get("title", "")]:
                if part:
                    remainder = remainder.replace(part, "", 1)
            remainder = AvvoEnricher._clean(remainder)
            if remainder:
                entry["description"] = remainder

            if entry:
                results.append(entry)
        return results

    async def parse_profile(
        self,
        url: str,
        lawyer: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Parse an Avvo lawyer profile page and return enriched data."""
        logger.info("Avvo parse start", url=url, lawyer=lawyer.get("full_name"))

        try:
            html = await self._fetch_html(url)
        except Exception as e:
            logger.error("Avvo profile fetch failed", url=url, error=str(e))
            return None

        soup = BeautifulSoup(html, "html.parser")

        # ── 1. JSON-LD (primary source for structured data) ───────────────────
        ld = self._extract_jsonld(soup, "LocalBusiness")

        bio         = self._clean(ld.get("description", "")) or None
        photo_url   = None
        if isinstance(ld.get("image"), dict):
            photo_url = ld["image"].get("url")
        elif isinstance(ld.get("image"), str):
            photo_url = ld["image"]

        # Social links from sameAs
        social_links: Dict[str, str] = {}
        for link in ld.get("sameAs", []):
            if "linkedin" in link:
                social_links["linkedin"] = link
            elif "twitter" in link or "x.com" in link:
                social_links["twitter"] = link
            elif "facebook" in link:
                social_links["facebook"] = link
            elif "instagram" in link:
                social_links["instagram"] = link

        # Practice areas
        practice_areas: List[str] = list(dict.fromkeys(
            self._clean(a) for a in ld.get("knowsAbout", []) if a
        ))

        # Phone
        phone = self._clean(ld.get("telephone", ""))
        if phone.lower() in {"not available", ""}:
            phone = ""

        # Address from LocalBusiness
        addr_obj  = ld.get("address", {})
        address   = {
            "line1":  self._clean(addr_obj.get("streetAddress", "")),
            "city":   self._clean(addr_obj.get("addressLocality", "")),
            "state":  self._clean(addr_obj.get("addressRegion", "")),
            "zip":    self._clean(addr_obj.get("postalCode", "")),
            "country": self._clean(addr_obj.get("addressCountry", "")),
        }
        address = {k: v for k, v in address.items() if v}

        # ── 2. Bio from HTML (JSON-LD may be truncated) ───────────────────────
        bio_html_el = soup.select_one(
            ".about-container .show-less-bio, "
            ".about-container #bio-content, "
            ".about-section-bio"
        )
        if bio_html_el:
            bio_html = self._clean(bio_html_el.get_text(" "))
            if bio_html and (not bio or len(bio_html) > len(bio)):
                bio = bio_html

        # ── 3. Practice areas from HTML (more reliable on the profile page) ──
        if not practice_areas:
            pa_section = soup.select_one(".practice-area-and-fees-section")
            if pa_section:
                for a in pa_section.select(".practice-area-title, .practice-area-title strong, a.practice-area-title"):
                    t = self._clean(a.get_text(" "))
                    if t:
                        practice_areas.append(t)
                practice_areas = list(dict.fromkeys(practice_areas))

        # ── 4. Ratings ────────────────────────────────────────────────────────
        client_rating   = None
        client_reviews  = None
        avvo_rating     = None

        review_score_el = soup.select_one(".rating-row .review-score span")
        if review_score_el:
            try:
                client_rating = float(review_score_el.get_text(strip=True))
            except ValueError:
                pass

        review_count_el = soup.select_one(".rating-row .review-score")
        if review_count_el:
            m = re.search(r"(\d+)\s*review", review_count_el.get_text(), re.I)
            if m:
                client_reviews = int(m.group(1))

        avvo_rating_el = soup.select_one(".avvo-ratings .avvo-rating-count span")
        if avvo_rating_el:
            m = re.search(r"([\d.]+)", avvo_rating_el.get_text())
            if m:
                try:
                    avvo_rating = float(m.group(1))
                except ValueError:
                    pass

        # ── 5. Education ──────────────────────────────────────────────────────
        education_history: List[Dict[str, Any]] = []
        edu_sec = soup.select_one(".education-container")
        if edu_sec:
            for exp in edu_sec.select(".experience"):
                entry: Dict[str, Any] = {}
                year_p  = exp.find("p")
                if year_p:
                    entry["year"] = self._clean(year_p.get_text())
                strong = exp.find("strong")
                if strong:
                    entry["school"] = self._clean(strong.get_text())
                # Degree is in the <p> after <strong>
                ps = exp.find_all("p")
                if len(ps) >= 2:
                    entry["degree"] = self._clean(ps[1].get_text())
                if entry:
                    education_history.append(entry)

        # ── 6. Work experience ────────────────────────────────────────────────
        work_history: List[Dict[str, Any]] = []
        work_sec = soup.select_one(".work-experience-container")
        if work_sec:
            for exp in work_sec.select(".experience"):
                entry: Dict[str, Any] = {}
                ps = exp.find_all("p")
                if ps:
                    entry["years"] = self._clean(ps[0].get_text())
                strong = exp.find("strong")
                if strong:
                    # Remove trailing comma / whitespace
                    entry["role"] = self._clean(strong.get_text()).rstrip(",").strip()
                    # Firm name is the text in the same <p> that is NOT in <strong>
                    if len(ps) >= 2:
                        # Extract text nodes that are direct children (not inside <strong>)
                        firm_parts = []
                        for node in ps[1].children:
                            if hasattr(node, "name") and node.name == "strong":
                                continue
                            firm_parts.append(str(node))
                        firm = self._clean(" ".join(firm_parts))
                        if firm:
                            entry["firm"] = firm
                else:
                    if len(ps) >= 2:
                        entry["firm"] = self._clean(ps[1].get_text())
                if entry:
                    work_history.append(entry)

        # ── 7. Professional associations ──────────────────────────────────────
        associations: List[Dict[str, Any]] = []
        assoc_sec = soup.select_one(".associations-container")
        if assoc_sec:
            associations = self._parse_experience_section(assoc_sec)

        # ── 8. Languages ──────────────────────────────────────────────────────
        languages: List[str] = []
        lang_sec = soup.select_one(".languages-container")
        if lang_sec:
            for p in lang_sec.select(".languages-list p"):
                t = self._clean(p.get_text())
                if t:
                    languages.append(t)

        # ── 9. Publications ───────────────────────────────────────────────────
        publications: List[Dict[str, Any]] = []
        pub_sec = soup.select_one(".publications-container")
        if pub_sec:
            publications = self._parse_experience_section(pub_sec)

        # ── 10. Websites (external links not in known social domains) ─────────
        SOCIAL_DOMAINS = {"facebook.com", "twitter.com", "x.com", "linkedin.com",
                          "instagram.com", "avvo.com", "justia.com"}
        websites: List[str] = []
        for a in soup.select("a[href]"):
            href = a.get("href", "")
            if not href.startswith("http"):
                continue
            domain = href.split("/")[2].lstrip("www.")
            if domain in SOCIAL_DOMAINS or "avvo.com" in domain:
                continue
            label = self._clean(a.get_text(" ")).lower()
            if "website" in label or "web site" in label or "visit" in label:
                websites.append(href)
        websites = list(dict.fromkeys(websites))[:5]

        # ── Assemble result ───────────────────────────────────────────────────
        result: Dict[str, Any] = {
            "bio":                  bio,
            "practice_areas":       practice_areas,
            "photo_url":            photo_url,
            "phone":                phone or None,
            "address":              address or None,
            "avvo_rating":          avvo_rating,
            "client_rating":        client_rating,
            "client_review_count":  client_reviews,
            "education_history":    education_history,
            "work_history":         work_history,
            "professional_associations": associations,
            "publications":         publications,
            "languages":            languages,
            "websites":             websites,
            "social_links":         social_links or None,
            "source_profile_url":   url,
        }

        # Drop None / empty values
        cleaned = {
            k: v for k, v in result.items()
            if v is not None
            and v != ""
            and not (isinstance(v, (list, dict)) and len(v) == 0)
        }

        logger.info(
            "Avvo parse complete",
            url=url,
            practice_areas=len(practice_areas),
            education=len(education_history),
            work=len(work_history),
            languages=len(languages),
            has_bio=bool(bio),
            avvo_rating=avvo_rating,
            client_rating=client_rating,
        )

        try:
            await self.close()
        except Exception:
            pass

        return cleaned or None
