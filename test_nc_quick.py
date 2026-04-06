"""Quick NC scraper verification — writes results to /tmp/nc_test_out.txt only."""
import asyncio
import sys
import logging

# Silence ALL logging before any imports
logging.disable(logging.CRITICAL)
for handler in logging.root.handlers[:]:
    logging.root.removeHandler(handler)

import os
os.environ.setdefault("LOG_LEVEL", "CRITICAL")

from playwright.async_api import async_playwright
from scrapers.states.north_carolina import StateBarNorthCarolina


async def main():
    out = []
    try:
        scraper = StateBarNorthCarolina(headless=True, checkpoint_enabled=False)
        async with async_playwright() as pw:
            browser = await pw.chromium.launch(headless=True)
            context = await browser.new_context()
            page = await context.new_page()

            # Navigate to the search page
            from scrapers.states.north_carolina import BASE_URL
            await page.goto(f"{BASE_URL}/Verification/search.aspx", wait_until="networkidle", timeout=30000)
            out.append(f"[OK] Page loaded: {page.url}")

            # Fill and submit with prefix "sm"
            await scraper._fill_and_submit(page, "sm")
            out.append("[OK] Form submitted successfully")

            # Count results
            count = await scraper._count_results(page)
            out.append(f"[OK] Result count: {count}")

            # Parse listing
            lawyers = []
            async for lawyer in scraper.parse_listing(page):
                lawyers.append(lawyer)

            out.append(f"[OK] Parsed lawyers: {len(lawyers)}")
            for i, lw in enumerate(lawyers[:5]):
                out.append(f"  [{i}] bar={lw.bar_number!r} name={lw.full_name!r} url={lw.detail_url!r}")

            await browser.close()
    except Exception as e:
        import traceback
        out.append(f"[ERROR] {e}")
        out.append(traceback.format_exc())

    result = "\n".join(out) + "\n"
    with open("/tmp/nc_test_out.txt", "w") as f:
        f.write(result)
    # Also print a tiny sentinel so we know the script finished
    print("DONE:" + str(len(out)))


asyncio.run(main())
