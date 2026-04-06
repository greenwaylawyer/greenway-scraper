"""
Export NY Bar Cloudflare cookies from your real Chrome browser.

Usage
-----
1. Open Chrome and visit https://iapps.courts.state.ny.us/attorney/AttorneySearch
2. Wait for Cloudflare to pass (it auto-passes in real Chrome within ~5 seconds)
3. Run this script:
       python scripts/export_ny_cookies.py
4. Copy the output file into Docker:
       docker cp output/ny_cf_cookies.json greenway_scraper:/app/output/ny_cf_cookies.json
5. Run the scraper normally:
       docker exec greenway_scraper python run.py --state new_york --test

The cf_clearance cookie is valid for roughly 1-24 hours. Repeat when it expires.
"""

import json
import os
import sys

TARGET_DOMAIN = "iapps.courts.state.ny.us"
OUTPUT_PATH = os.path.join(os.path.dirname(__file__), "..", "output", "ny_cf_cookies.json")


def _sameSite_map(value: str) -> str:
    """Normalise sameSite values to what Playwright accepts."""
    v = (value or "").lower()
    if v in ("strict",):
        return "Strict"
    if v in ("lax",):
        return "Lax"
    return "None"


def export_from_browser_cookie3() -> list:
    """Read cookies from Chrome using browser-cookie3."""
    try:
        import browser_cookie3
    except ImportError:
        print("browser-cookie3 not installed. Installing…")
        os.system(f"{sys.executable} -m pip install browser-cookie3")
        import browser_cookie3

    jar = browser_cookie3.chrome(domain_name=TARGET_DOMAIN)
    cookies = []
    for c in jar:
        cookies.append({
            "name": c.name,
            "value": c.value,
            "domain": c.domain.lstrip(".") if c.domain else TARGET_DOMAIN,
            "path": c.path or "/",
            "expires": int(c.expires) if c.expires else -1,
            "httpOnly": bool(c.has_nonstandard_attr("HttpOnly")),
            "secure": bool(c.secure),
            "sameSite": _sameSite_map(c.get_nonstandard_attr("SameSite", "")),
        })
    return cookies


def export_manually() -> list:
    """Fallback: ask user to paste cookie values manually."""
    print("\nbrowser-cookie3 could not read Chrome cookies automatically.")
    print("Manual method:")
    print("  1. Open Chrome DevTools on the NY Bar page (F12)")
    print("  2. Application → Cookies → https://iapps.courts.state.ny.us")
    print("  3. Find 'cf_clearance' and copy its Value")
    cf = input("\nPaste cf_clearance value (or press Enter to skip): ").strip()

    cookies = []
    if cf:
        cookies.append({
            "name": "cf_clearance",
            "value": cf,
            "domain": TARGET_DOMAIN,
            "path": "/",
            "expires": -1,
            "httpOnly": False,
            "secure": True,
            "sameSite": "None",
        })
    return cookies


def main():
    print(f"Exporting Cloudflare cookies for {TARGET_DOMAIN}…")

    cookies = []
    try:
        cookies = export_from_browser_cookie3()
        cf_names = [c["name"] for c in cookies]
        print(f"Found {len(cookies)} cookies: {cf_names}")
        if "cf_clearance" not in cf_names:
            print(
                "\nWARNING: cf_clearance not found.\n"
                "Make sure you visited the NY Bar site in Chrome and the\n"
                "Cloudflare challenge completed before running this script."
            )
    except Exception as e:
        print(f"browser-cookie3 failed: {e}")
        cookies = export_manually()

    if not cookies:
        print("No cookies exported. Exiting.")
        sys.exit(1)

    os.makedirs(os.path.dirname(OUTPUT_PATH), exist_ok=True)
    with open(OUTPUT_PATH, "w") as f:
        json.dump(cookies, f, indent=2)

    abs_path = os.path.abspath(OUTPUT_PATH)
    print(f"\nSaved {len(cookies)} cookies → {abs_path}")
    print("\nNext steps:")
    print(f"  docker cp {abs_path} greenway_scraper:/app/output/ny_cf_cookies.json")
    print("  docker exec greenway_scraper python run.py --state new_york --test")


if __name__ == "__main__":
    main()
