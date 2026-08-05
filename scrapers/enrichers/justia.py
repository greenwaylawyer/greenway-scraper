"""
Justia.com enricher for Layer 3.

Status: STUB — Requires manual research of Justia HTML structure.

TODO before enabling:
1. Visit https://www.justia.com/search and understand search result format
2. Find 10-20 sample lawyer profiles and document HTML structure
3. Identify CSS selectors for: bio, practice_areas, education_history, 
   photo_url, websites, languages, social_links
4. Save sample HTML to tests/fixtures/justia/
5. Implement discover_profile() and parse_profile() methods below
6. Write unit tests in tests/test_justia_enricher.py
7. Test on 5-10 real profiles from staging database
8. Enable in config/enrichment_sources.yaml (enabled: true)
"""

import asyncio
import os
import random
from typing import Dict, Any, List, Optional
import re
from urllib.parse import urlencode, urljoin

from bs4 import BeautifulSoup

from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
from utils.logger import get_logger

logger = get_logger(__name__)

# FlareSolverr endpoint — set FLARESOLVERR_URL in .env
# Falls back to direct Playwright if not configured.
FLARESOLVERR_URL = os.getenv("FLARESOLVERR_URL", "")


async def _fetch_via_flaresolverr(url: str, timeout_ms: int = 60_000) -> str:
    """
    Fetch a Cloudflare-protected URL through FlareSolverr.

    FlareSolverr starts a real Chrome session, solves the CF challenge, then
    returns the final HTML.  Requires the `flaresolverr` Docker container.
    """
    import aiohttp

    payload = {"cmd": "request.get", "url": url, "maxTimeout": timeout_ms}
    endpoint = FLARESOLVERR_URL.rstrip("/") + "/v1"

    async with aiohttp.ClientSession() as session:
        async with session.post(endpoint, json=payload, timeout=aiohttp.ClientTimeout(total=timeout_ms / 1000 + 10)) as resp:
            data = await resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"FlareSolverr error: {data.get('message', data)}")

    return data["solution"]["response"]


class JustiaEnricher(BaseEnricher):
    """Justia.com enrichment source for Layer 3."""

    BASE_URL = "https://lawyers.justia.com"

    # Realistic Chrome UA — rotated per-request to reduce fingerprint detection
    _USER_AGENTS = [
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
        "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/121.0.0.0 Safari/537.36",
    ]

    # Playwright browser/context shared across requests in one enricher instance
    _playwright = None
    _browser    = None

    def get_source_key(self) -> str:
        return 'justia'

    async def _get_browser(self):
        """Lazily start a shared Playwright Chromium browser."""
        if self._browser is None:
            from playwright.async_api import async_playwright
            self._playwright = await async_playwright().start()
            self._browser = await self._playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-blink-features=AutomationControlled",
                ],
            )
            logger.info("Playwright Chromium browser started")
        return self._browser

    async def close(self) -> None:
        """Shut down the shared browser when done."""
        if self._browser:
            await self._browser.close()
            self._browser = None
        if self._playwright:
            await self._playwright.stop()
            self._playwright = None

    async def _fetch_html(self, url: str, wait_ms: int = 1200) -> str:
        """
        Fetch a Cloudflare-protected page.

        Strategy (in priority order):
        1. FlareSolverr — if FLARESOLVERR_URL is set in .env this is used first.
           It runs a real Chrome session internally and reliably solves CF
           managed challenges.
        2. Direct Playwright fallback — used when FlareSolverr is not available
           (e.g. local testing without the container). Works for sites without
           aggressive bot protection but may fail against CF managed challenges.
        """
        if FLARESOLVERR_URL:
            try:
                html = await _fetch_via_flaresolverr(url)
                logger.info("FlareSolverr fetch OK", url=url, length=len(html))
                return html
            except Exception as e:
                logger.warning("FlareSolverr failed, falling back to Playwright", url=url, error=str(e))

        # ── Playwright fallback ──────────────────────────────────────────────
        try:
            from playwright_stealth import Stealth
            _stealth = Stealth(navigator_webdriver=True)
        except ImportError:
            _stealth = None

        browser = await self._get_browser()
        ua = random.choice(self._USER_AGENTS)

        context = await browser.new_context(
            user_agent=ua,
            viewport={"width": 1440, "height": 900},
            locale="en-US",
            timezone_id="America/New_York",
            extra_http_headers={
                "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
                "Accept-Language":           "en-US,en;q=0.9",
                "Accept-Encoding":           "gzip, deflate, br",
                "DNT":                       "1",
                "Upgrade-Insecure-Requests": "1",
            },
        )

        page = await context.new_page()
        if _stealth:
            await _stealth.apply_stealth_async(page)

        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=45_000)

            for _ in range(12):
                title = await page.title()
                if "just a moment" not in title.lower():
                    break
                logger.info("Cloudflare challenge in progress, waiting…", url=url)
                await asyncio.sleep(1)
            else:
                logger.warning("Cloudflare challenge did not clear — page may be incomplete", url=url)

            await asyncio.sleep(wait_ms / 1000 + random.uniform(0.1, 0.4))
            html = await page.content()
        finally:
            await context.close()

        return html

    @staticmethod
    def _clean_text(s: str) -> str:
        return re.sub(r"\s+", " ", (s or "")).strip()

    @staticmethod
    def _extract_bar_number_from_text(text: str) -> Optional[str]:
        if not text:
            return None
        m = re.search(r"State Bar\s*#\s*([0-9]{4,})", text, flags=re.IGNORECASE)
        return m.group(1) if m else None

    def _build_search_url(self, lawyer: Dict[str, Any]) -> str:
        name = lawyer.get("full_name") or " ".join(
            [lawyer.get("first_name", ""), lawyer.get("last_name", "")]
        ).strip()
        location = lawyer.get("city") or lawyer.get("license_state") or lawyer.get("state") or ""

        params = {
            "profile-id-field": "",
            "practice-id-field": "",
            "service-id-field": "",
            "query": name,
            "location": location,
        }
        return f"{self.BASE_URL}/search?{urlencode(params)}"

    @staticmethod
    def _state_code_to_name(code: str) -> Optional[str]:
        if not code:
            return None
        code = code.strip().upper()
        return {
            "NY": "New York",
            "CA": "California",
            "TX": "Texas",
            "FL": "Florida",
            "IL": "Illinois",
            "PA": "Pennsylvania",
            "OH": "Ohio",
            "GA": "Georgia",
            "NC": "North Carolina",
            "MI": "Michigan",
            "NJ": "New Jersey",
            "VA": "Virginia",
            "WA": "Washington",
            "AZ": "Arizona",
            "MA": "Massachusetts",
        }.get(code)

    @staticmethod
    def _parse_search_candidates(html: str) -> List[DiscoveryCandidate]:
        """
        Extract DiscoveryCandidate objects from Justia search results HTML.

        Strategy: collect the best (name, location) per unique profile URL,
        then build candidates.  A plain image-wrapper link with empty text
        must NOT block a later link to the same URL that carries the real name.
        """
        soup = BeautifulSoup(html, "html.parser")

        # url → (name, location) — keep the entry with the longest name
        url_to_best: Dict[str, tuple] = {}

        for a in soup.select('a[href*="/lawyer/"]'):
            href = a.get("href") or ""
            if not href:
                continue
            # Skip non-profile sub-pages
            if any(x in href for x in ["client-review", "contact", "vcard", "#"]):
                continue

            url = href if href.startswith("http") else urljoin("https://lawyers.justia.com", href)

            name = JustiaEnricher._clean_text(a.get_text(" ", strip=True))
            # Skip icon/image-only links; also skip "View LawyerProfile", "Email Lawyer" etc.
            skip_texts = {"view lawyerprofile", "email lawyer", "call lawyer", "contact"}
            if not name or len(name) < 3 or name.lower().replace(" ", "") in {t.replace(" ", "") for t in skip_texts}:
                # Still record the URL so we don't miss it if no better link exists
                if url not in url_to_best:
                    url_to_best[url] = ("", "")
                continue

            location = ""
            container = a.find_parent(["li", "div", "article"]) or a.parent
            if container:
                container_text = container.get_text("\n", strip=True)
                lines = [JustiaEnricher._clean_text(ln) for ln in container_text.split("\n") if ln.strip()]
                for line in reversed(lines):
                    if any(ch.isalpha() for ch in line) and len(line) <= 64:
                        location = line
                        break

            existing_name = url_to_best.get(url, ("", ""))[0]
            if len(name) > len(existing_name):
                url_to_best[url] = (name, location)

        candidates = [
            DiscoveryCandidate(url=url, name=name, location=location)
            for url, (name, location) in url_to_best.items()
            if name  # drop URLs where we only saw empty/skip-only links
        ]

        return candidates

    async def discover_profile(
        self,
        lawyer: Dict[str, Any],
    ) -> List[DiscoveryCandidate]:
        """
        Search Justia for matching lawyer profile.
        
        TODO: Implement the following strategies:
        1. Name + city + state search
        2. Bar number search (if supported by Justia for this state)
        
        For each result, extract:
        - profile URL
        - name displayed
        - location displayed
        - bar number (if shown in search results)
        
        Score each candidate using self.score_candidate()
        
        Args:
            lawyer: Dict with full_name, first_name, last_name, city, state, bar_number
        
        Returns:
            List of DiscoveryCandidate objects
        """
        # We try multiple location strategies because some records may have
        # missing/incorrect city values (or city from firm address vs license state).
        city = (lawyer.get("city") or "").strip()
        state_code = (lawyer.get("license_state") or lawyer.get("state") or "").strip()
        state_name = self._state_code_to_name(state_code)

        locations_to_try: List[str] = []
        if city:
            locations_to_try.append(city)
        if state_name:
            locations_to_try.append(state_name)
        if state_code:
            locations_to_try.append(state_code)
        locations_to_try.append("")  # fallback: no location filter

        candidates_map: Dict[str, DiscoveryCandidate] = {}

        for loc in locations_to_try:
            search_lawyer = dict(lawyer)
            search_lawyer["city"] = loc if loc else ""

            search_url = self._build_search_url(search_lawyer)
            logger.info(
                "Justia discovery search",
                url=search_url,
                lawyer=lawyer.get("full_name"),
                location=loc or "(none)",
                bar_number=lawyer.get("bar_number"),
            )

            try:
                html = await self._fetch_html(search_url)
            except Exception as e:
                logger.error("Justia search fetch failed", url=search_url, error=str(e))
                continue

            new_candidates = [
                c for c in self._parse_search_candidates(html)
                if c.url not in candidates_map
            ]
            for c in new_candidates:
                candidates_map[c.url] = c

            # Stop trying more locations if we already found a strong match
            exact = [c for c in candidates_map.values() if c.bar_number == str(lawyer.get("bar_number") or "")]
            if exact:
                logger.info("Justia: bar number match found early, stopping location loop")
                break

            # Short polite delay between search pages
            await asyncio.sleep(random.uniform(0.8, 1.5))

        candidates = list(candidates_map.values())

        # Fetch top candidate profiles to get bar number for auto-merge
        max_profile_fetches = 5
        for candidate in candidates[:max_profile_fetches]:
            try:
                profile_html = await self._fetch_html(candidate.url, wait_ms=1000)
                profile_text = BeautifulSoup(profile_html, "html.parser").get_text("\n", strip=True)
                candidate.bar_number = self._extract_bar_number_from_text(profile_text)
            except Exception as e:
                logger.warning("Justia candidate fetch failed", url=candidate.url, error=str(e))

            self.score_candidate(candidate, lawyer)

            # Short polite delay between profile fetches
            await asyncio.sleep(random.uniform(0.5, 1.2))

        for candidate in candidates[max_profile_fetches:]:
            self.score_candidate(candidate, lawyer)

        candidates.sort(key=lambda c: c.confidence, reverse=True)

        logger.info(
            "Justia discovery completed",
            total=len(candidates),
            top_confidence=candidates[0].confidence if candidates else 0,
            top_url=candidates[0].url if candidates else None,
            top_bar=candidates[0].bar_number if candidates else None,
        )

        try:
            await self.close()
        except Exception:
            pass

        return candidates

    async def parse_profile(
        self,
        url: str,
        lawyer: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Parse a Justia lawyer profile page.
        
        TODO: Navigate to the profile URL and extract:
        - bio (text from bio section)
        - practice_areas (list of practice area strings)
        - education_history (list of dicts: {school, degree, year})
        - photo_url (string)
        - websites (list of URLs)
        - languages (list of language strings)
        - social_links (dict: {platform: url})
        
        Args:
            url: Direct URL to Justia profile
            lawyer: Context about lawyer being enriched
        
        Returns:
            Dict with extracted fields or None if page not found/no data
        """
        logger.info("Justia parse start", url=url, lawyer=lawyer.get("full_name"))

        try:
            html = await self._fetch_html(url)
        except Exception as e:
            logger.error("Justia profile fetch failed", url=url, error=str(e))
            return None

        soup = BeautifulSoup(html, "html.parser")
        text = soup.get_text("\n", strip=True)

        def section_between(start: str, end: str) -> str:
            pattern = rf"{re.escape(start)}\n(.*?)(?:\n{re.escape(end)}\n|$)"
            m = re.search(pattern, text, flags=re.IGNORECASE | re.DOTALL)
            return (m.group(1).strip() if m else "").strip()

        bio = section_between("Biography", "Practice Areas") or section_between("Biography", "Fees")

        practice_raw = section_between("Practice Areas", "Fees") or section_between(
            "Practice Areas", "Jurisdictions Admitted to Practice"
        )
        practice_areas: List[str] = []
        if practice_raw:
            for line in [self._clean_text(l) for l in practice_raw.split("\n")]:
                if not line:
                    continue
                parts = re.split(r"\s{2,}|•|\|", line)
                for p in parts:
                    p = self._clean_text(p)
                    if p:
                        practice_areas.append(p)
        practice_areas = list(dict.fromkeys(practice_areas))

        languages_raw = section_between("Languages", "Professional Experience") or section_between("Languages", "Education")
        languages: List[str] = []
        if languages_raw:
            for line in languages_raw.split("\n"):
                line = self._clean_text(line)
                if not line:
                    continue
                lang = line.split(":")[0].strip()
                if lang and len(lang) <= 30:
                    languages.append(lang)
        languages = list(dict.fromkeys(languages))

        websites: List[str] = []
        social_links: Dict[str, str] = {}
        for a in soup.select("a[href]"):
            href = a.get("href") or ""
            if not href.startswith("http"):
                continue
            if "justia.com" in href:
                continue
            label = self._clean_text(a.get_text(" ", strip=True)).lower()
            if "linkedin" in href:
                social_links["linkedin"] = href
            elif "twitter" in href or "x.com" in href:
                social_links["twitter"] = href
            elif "facebook" in href:
                social_links["facebook"] = href
            elif "instagram" in href:
                social_links["instagram"] = href
            elif "avvo.com" in href:
                social_links["avvo"] = href
            elif label in {"website", "blog"} or "website" in label or "blog" in label:
                websites.append(href)

        if not websites:
            externals: List[str] = []
            for a in soup.select("a[href]"):
                href = a.get("href") or ""
                if href.startswith("http") and "justia.com" not in href:
                    externals.append(href)
            websites = list(dict.fromkeys(externals))[:5]

        bar_number = self._extract_bar_number_from_text(text)

        photo_url = None
        img = soup.select_one("img[src]")
        if img and img.get("src"):
            src = img["src"]
            if "profile" in src or "justia" in src:
                photo_url = src if src.startswith("http") else urljoin(self.BASE_URL, src)

        education_raw = section_between("Education", "Professional Associations") or section_between("Education", "Websites & Blogs")
        education_history: List[Dict[str, Any]] = []
        if education_raw:
            lines = [self._clean_text(l) for l in education_raw.split("\n") if l.strip()]
            if lines:
                education_history.append({"school": lines[0]})

        result: Dict[str, Any] = {
            "bio": bio or None,
            "practice_areas": practice_areas,
            "education_history": education_history,
            "photo_url": photo_url,
            "websites": websites,
            "languages": languages,
            "social_links": social_links,
            "bar_number_on_source": bar_number,
            "source_profile_url": url,
        }

        cleaned: Dict[str, Any] = {}
        for k, v in result.items():
            if v is None:
                continue
            if isinstance(v, (list, dict)) and len(v) == 0:
                continue
            cleaned[k] = v

        logger.info(
            "Justia parse complete",
            url=url,
            found_bar_number=bar_number,
            practice_areas=len(practice_areas),
            languages=len(languages),
            websites=len(websites),
            has_bio=bool(bio),
        )

        try:
            await self.close()
        except Exception:
            pass

        return cleaned if len(cleaned) > 1 or 'source_profile_url' not in cleaned else None


# Placeholder for testing
if __name__ == '__main__':
    import asyncio
    
    async def test():
        enricher = JustiaEnricher()
        
        sample_lawyer = {
            'full_name': 'John Smith',
            'first_name': 'John',
            'last_name': 'Smith',
            'city': 'Los Angeles',
            'state': 'CA',
            'bar_number': '123456',
        }
        
        print("Testing Justia discovery...")
        candidates = await enricher.discover_profile(sample_lawyer)
        print(f"Found {len(candidates)} candidates")
        
        if candidates:
            print("\nTesting Justia parse_profile...")
            data = await enricher.parse_profile(candidates[0].url, sample_lawyer)
            print(f"Extracted data: {data}")
    
    asyncio.run(test())
