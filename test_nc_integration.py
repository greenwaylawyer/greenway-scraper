#!/usr/bin/env python3
"""
NC scraper integration test — one prefix ('sm') through the real scraper class.
Output written to /tmp/nc_integration_result.txt so VIEWSTATE never reaches stdout.
"""
import asyncio
import sys
import traceback
sys.path.insert(0, '/app')

OUTPUT = '/tmp/nc_integration_result.txt'

async def main():
    lines = []
    try:
        from playwright.async_api import async_playwright
        from scrapers.states.north_carolina import StateBarNorthCarolina

        # Monkey-patch run() to do only ONE prefix instead of aa->zz
        TEST_PREFIX = 'sm'   # 'sm' is a high-density prefix, good stress test

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            context = await browser.new_context(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )
            page = await context.new_page()

            scraper = StateBarNorthCarolina(
                headless=True,
                checkpoint_enabled=False,
            )
            scraper.browser = browser
            scraper.context = context
            scraper.page = page

            lines.append(f"=== NC Scraper integration test: prefix='{TEST_PREFIX}' ===\n")

            # Step 1: fill and submit
            lines.append("STEP 1: _fill_and_submit")
            await scraper._fill_and_submit(page, TEST_PREFIX)
            lines.append(f"  URL after submit: {page.url}")

            # Step 2: count results
            count = await scraper._count_results(page)
            lines.append(f"  _count_results returned: {count}")

            # Step 3: parse listing
            lines.append("\nSTEP 2: parse_listing")
            lawyers = []
            async for lawyer in scraper.parse_listing(page):
                lawyers.append(lawyer)

            lines.append(f"  Total parsed: {len(lawyers)}")
            lines.append("\nFirst 10 lawyers:")
            for lw in lawyers[:10]:
                lines.append(
                    f"  bar={lw.bar_number!r:8s}  "
                    f"name={lw.full_name!r:40s}  "
                    f"status={lw.license_status!r}  "
                    f"url={lw.detail_url!r}"
                )

            if not lawyers:
                lines.append("\n  !! NO LAWYERS PARSED — checking raw page content:")
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(await page.content(), "html.parser")
                table = scraper._find_results_table(soup)
                if table:
                    rows = table.find_all("tr")
                    lines.append(f"  Table found, {len(rows)} rows total")
                    for r in rows[:3]:
                        lines.append(f"    {r.get_text(' ', strip=True)[:120]}")
                else:
                    lines.append("  No results table found either!")
                    body = await page.inner_text("body")
                    lines.append(f"  Body text (first 500 chars):\n{body[:500]}")

            await browser.close()

    except Exception as e:
        lines.append(f"\nFATAL ERROR: {e}")
        lines.append(traceback.format_exc())

    output = '\n'.join(lines)
    with open(OUTPUT, 'w') as f:
        f.write(output)
    # Print a brief summary only (no VIEWSTATE)
    for line in lines:
        if not line.startswith('  id=') and '__VIEWSTATE' not in line:
            print(line)

asyncio.run(main())
