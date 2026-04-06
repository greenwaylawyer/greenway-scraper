#!/usr/bin/env python3
"""
NC scraper integration test — runs ONE prefix ('sm') through the real scraper class
and prints what it found.  Results also written to /tmp/nc_test_result.txt.
"""
import asyncio
import sys
sys.path.insert(0, '/app')

OUTPUT = '/tmp/nc_test_result.txt'

async def main():
    lines = []
    try:
        from playwright.async_api import async_playwright
        from bs4 import BeautifulSoup

        SEARCH_URL = "https://portal.ncbar.gov/Verification/search.aspx"
        BASE_URL   = "https://portal.ncbar.gov"

        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page    = await browser.new_page(
                user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
            )

            lines.append("=== STEP 1: Navigate to search page ===")
            await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
            lines.append(f"URL after nav: {page.url}")

            # Dump all input fields and selects to identify real IDs
            lines.append("\n=== STEP 2: All form inputs ===")
            inputs = await page.query_selector_all('input, select, textarea')
            for el in inputs:
                el_id   = await el.get_attribute('id')   or ''
                el_name = await el.get_attribute('name') or ''
                el_type = await el.get_attribute('type') or await el.evaluate("el => el.tagName")
                el_val  = await el.get_attribute('value') or ''
                lines.append(f"  id={el_id!r:30s}  name={el_name!r:30s}  type={el_type}  value={el_val!r}")

            # Try filling Last Name
            lines.append("\n=== STEP 3: Fill form ===")
            last_input = await page.query_selector('#txtLast')
            if last_input:
                await last_input.fill('aa')
                lines.append("Filled #txtLast with 'aa'")
            else:
                lines.append("ERROR: #txtLast not found!")
                # Try alternatives
                for sel in ['input[name*="Last"]', 'input[name*="last"]', 'input[id*="Last"]']:
                    el = await page.query_selector(sel)
                    if el:
                        eid = await el.get_attribute('id')
                        lines.append(f"  Found alternative: {sel}  id={eid}")

            status_sel = await page.query_selector('#ddLicStatus')
            if status_sel:
                await status_sel.select_option(value='A')
                lines.append("Set #ddLicStatus to 'A' (Active)")
            else:
                lines.append("WARNING: #ddLicStatus not found")
                for sel in ['select[name*="Status"]', 'select[id*="Status"]', 'select[id*="Lic"]']:
                    el = await page.query_selector(sel)
                    if el:
                        eid = await el.get_attribute('id')
                        lines.append(f"  Found alternative: {sel}  id={eid}")

            # Submit
            lines.append("\n=== STEP 4: Submit ===")
            btn = await page.query_selector('#btnSubmit')
            if btn:
                await btn.click()
                lines.append("Clicked #btnSubmit")
            else:
                lines.append("ERROR: #btnSubmit not found, trying fallback")
                btn2 = await page.query_selector('input[type="submit"]')
                if btn2:
                    val = await btn2.get_attribute('value')
                    bid = await btn2.get_attribute('id')
                    lines.append(f"  Fallback submit: id={bid!r}  value={val!r}")
                    await btn2.click()

            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(1000)
            lines.append(f"URL after submit: {page.url}")

            # Check for results
            lines.append("\n=== STEP 5: Parse results ===")
            soup = BeautifulSoup(await page.content(), "html.parser")

            # Find all tables and their header text
            for i, tbl in enumerate(soup.find_all("table")):
                headers = [th.get_text(strip=True) for th in tbl.find_all("th")]
                data_rows = [r for r in tbl.find_all("tr") if r.find_all("td")]
                lines.append(f"  Table[{i}]: headers={headers}  data_rows={len(data_rows)}")

            # Try Bootstrap table-hover (expected)
            result_table = soup.find("table", class_=lambda c: c and "table-hover" in c)
            if result_table:
                data_rows = [r for r in result_table.find_all("tr") if r.find_all("td")]
                lines.append(f"\n  Found table-hover with {len(data_rows)} data rows")
                for row in data_rows[:3]:
                    cols = row.find_all("td")
                    col_texts = [c.get_text(" ", strip=True) for c in cols]
                    link = cols[1].find("a", href=True) if len(cols) > 1 else None
                    href = link["href"] if link else None
                    lines.append(f"    cols={col_texts}  href={href!r}")
            else:
                lines.append("\n  No table-hover found. Dumping page text (first 2000 chars):")
                text = await page.inner_text("body")
                lines.append(text[:2000])

            await browser.close()

    except Exception as e:
        import traceback
        lines.append(f"\nFATAL ERROR: {e}")
        lines.append(traceback.format_exc())

    with open(OUTPUT, 'w') as f:
        f.write('\n'.join(lines))
    print(f"Done. Output written to {OUTPUT}")

asyncio.run(main())
