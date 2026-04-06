"""
Offline diagnostic for CA Bar detail parsing — NO PLAYWRIGHT.

Uses BeautifulSoup only so the test never hangs on external resources.
Calls the same _from_html parser methods the production enricher uses.

Run:  docker exec greenway_scraper python /app/test_ca_detail.py
"""
import re, sys
from pathlib import Path
from bs4 import BeautifulSoup

# ── allow imports from project root ──
sys.path.insert(0, str(Path(__file__).parent))

from scrapers.enrichers.calbar_details import CaliforniaDetailEnricher

BAR_NUMBER = "33269"
SAMPLE = "/app/sample-webpage-data/apps.calbar.ca.gov/Julius Aarons # 33269 - Attorney Licensee Search.html"


def main():
    html = Path(SAMPLE).read_text(encoding="utf-8")
    print(f"Loaded sample HTML: {len(html)} bytes")

    soup = BeautifulSoup(html, "html.parser")
    text = soup.get_text("\n", strip=True)
    print(f"Full text length: {len(text)} chars")

    # ── Show key field snippets from raw text ──────────────────────
    for keyword in [
        "Address:", "Phone:", "Fax:", "Email:", "Website:",
        "Law School:", "Self-Reported Practice Areas:",
        "License Status:", "Additional Languages Spoken:",
        "Admitted to the State Bar",
    ]:
        idx = text.find(keyword)
        if idx >= 0:
            snippet = text[idx : idx + 200].replace("\n", " | ").strip()
            print(f"  FOUND '{keyword}' => {snippet[:140]}")
        else:
            print(f"  MISSING '{keyword}'")

    # ── Instantiate enricher and test each HTML parser method ──────
    e = CaliforniaDetailEnricher()

    print("\n=== HTML-based parsers ===")
    print(f"license_status: {e._parse_license_status_from_html(soup)}")
    print(f"address:        {e._parse_address_from_html(soup)}")
    print(f"phone:          {e._parse_phone_from_html(soup)}")
    print(f"fax:            {e._parse_fax_from_html(soup)}")
    print(f"email (html):   {e._parse_email_from_html(soup, 'Julius Aarons')}")
    print(f"website:        {e._parse_website_from_html(soup)}")
    print(f"law_school:     {e._parse_law_school_from_html(soup)}")
    print(f"practice_areas: {e._parse_practice_areas_from_html(soup)}")
    print(f"languages:      {e._parse_languages_from_html(soup)}")

    print("\n=== Text-based parsers (fallback) ===")
    print(f"license_status: {e._parse_license_status(text)}")
    print(f"address:        {e._parse_address(text)}")
    print(f"phone:          {e._parse_phone(text)}")
    print(f"fax:            {e._parse_fax(text)}")
    print(f"email:          {e._parse_email(text)}")
    print(f"website:        {e._parse_website(text)}")
    print(f"law_school:     {e._parse_law_school(text)}")
    print(f"practice_areas: {e._parse_practice_areas(text)}")
    print(f"admission_year: {e._parse_admission_year(text)}")
    print(f"languages:      {e._parse_languages(text)}")

    # ── Name extraction (regex from text — no Playwright needed) ──
    name_match = re.search(r'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)*)\s*#' + BAR_NUMBER, html)
    print(f"\nname from HTML: {name_match.group(1).strip() if name_match else 'NOT FOUND'}")

    # ── Verdict ───────────────────────────────────────────────────
    results = {
        "address": e._parse_address_from_html(soup) or e._parse_address(text),
        "phone": e._parse_phone_from_html(soup) or e._parse_phone(text),
        "email": e._parse_email_from_html(soup, "Julius Aarons") or e._parse_email(text),
        "law_school": e._parse_law_school_from_html(soup) or e._parse_law_school(text),
        "practice_areas": e._parse_practice_areas_from_html(soup) or e._parse_practice_areas(text),
    }
    missing = [k for k, v in results.items() if not v]
    print(f"\n{'MISSING: ' + ', '.join(missing) if missing else 'ALL KEY FIELDS PRESENT'}")


if __name__ == "__main__":
    main()
