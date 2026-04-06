#!/usr/bin/env python3
# REWRITTEN DEBUG SCRIPT
"""
Debug script: open NC Bar search page, dump ALL form inputs,
try first search, dump result HTML.

Usage (inside container):
    python debug_nc.py
"""
import asyncio
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).parent))

from playwright.async_api import async_playwright

SEARCH_URL = "https://portal.ncbar.gov/Verification/search.aspx"


async def main():
    async with async_playwright() as pw:
        browser = await pw.chromium.launch(headless=True)
        ctx = await browser.new_context(
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = await ctx.new_page()

        print(f"\n{'='*60}")
        print(f"Loading: {SEARCH_URL}")
        print('='*60)

        await page.goto(SEARCH_URL, wait_until="networkidle", timeout=30000)
        await page.wait_for_timeout(1500)

        # ── 1. Dump every <input> on the page ────────────────────────────────
        print("\n--- ALL <input> elements ---")
        inputs = await page.query_selector_all("input")
        for inp in inputs:
            id_   = await inp.get_attribute("id") or ""
            name_ = await inp.get_attribute("name") or ""
            type_ = await inp.get_attribute("type") or ""
            value_= await inp.get_attribute("value") or ""
            placeholder = await inp.get_attribute("placeholder") or ""
            print(f"  id={id_!r:60s}  name={name_!r:60s}  type={type_!r}  value={value_!r}  placeholder={placeholder!r}")

        # ── 2. Dump every <select> ─────────────────────────────────────────────
        print("\n--- ALL <select> elements ---")
        selects = await page.query_selector_all("select")
        for sel in selects:
            id_   = await sel.get_attribute("id") or ""
            name_ = await sel.get_attribute("name") or ""
            print(f"  id={id_!r:60s}  name={name_!r}")
            # Show options
            opts = await sel.query_selector_all("option")
            for opt in opts:
                val  = await opt.get_attribute("value") or ""
                text = (await opt.text_content() or "").strip()
                print(f"      option value={val!r}  text={text!r}")

        # ── 3. Screenshot before search ────────────────────────────────────────
        await page.screenshot(path="/tmp/nc_before_search.png", full_page=True)
        print("\n[Screenshot saved: /tmp/nc_before_search.png]")

        # ── 4. Try filling the last-name field using every heuristic ───────────
        print("\n--- Trying to fill Last Name field ---")

        # Try by partial ID match
        for attempt_sel in [
            'input[id$="LastName"]',
            'input[id$="txtLastName"]',
            'input[id*="Last"]',
            'input[id*="last"]',
            'input[name*="LastName"]',
            'input[name*="last"]',
        ]:
            el = await page.query_selector(attempt_sel)
            if el:
                print(f"  FOUND with: {attempt_sel!r}")
                await el.fill("sm")
                break
        else:
            # Last resort: dump raw HTML around the form
            print("  NOT FOUND with any selector — dumping form HTML")
            form_html = await page.inner_html("form") if await page.query_selector("form") else await page.content()
            # Only print first 4000 chars to keep it readable
            print(form_html[:4000])

        # ── 5. Try setting Member Status = Active ─────────────────────────────
        print("\n--- Trying Member Status select ---")
        for attempt_sel in [
            'select[id$="ddlMemberStatus"]',
            'select[id$="MemberStatus"]',
            'select[id*="Status"]',
            'select[id*="status"]',
            'select[name*="Status"]',
        ]:
            el = await page.query_selector(attempt_sel)
            if el:
                print(f"  FOUND with: {attempt_sel!r}")
                try:
                    await el.select_option(label="Active")
                    print("  Set to 'Active'")
                except Exception as e:
                    print(f"  select_option failed: {e}")
                    # Print options
                    opts = await el.query_selector_all("option")
                    for opt in opts:
                        val = await opt.get_attribute("value") or ""
                        txt = (await opt.text_content() or "").strip()
                        print(f"    option value={val!r}  text={txt!r}")
                break
        else:
            print("  Member Status select NOT FOUND with any selector")

        # ── 6. Hit search ─────────────────────────────────────────────────────
        print("\n--- Submitting form ---")
        search_btn = await page.query_selector('input[type="submit"]')
        if search_btn:
            btn_val = await search_btn.get_attribute("value") or ""
            btn_id  = await search_btn.get_attribute("id") or ""
            print(f"  Found submit button: id={btn_id!r}  value={btn_val!r}")
            await search_btn.click()
            await page.wait_for_load_state("networkidle", timeout=30000)
            await page.wait_for_timeout(1500)
        else:
            print("  No submit button found — trying Enter key")
            await page.keyboard.press("Enter")
            await page.wait_for_load_state("networkidle", timeout=30000)

        # ── 7. Screenshot after search ─────────────────────────────────────────
        await page.screenshot(path="/tmp/nc_after_search.png", full_page=True)
        print("[Screenshot saved: /tmp/nc_after_search.png]")

        # ── 8. Dump result area HTML ──────────────────────────────────────────
        print("\n--- Result area HTML (first 5000 chars) ---")
        html = await page.content()

        # Find any table
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        tables = soup.find_all("table")
        print(f"  Total <table> elements on results page: {len(tables)}")
        for i, t in enumerate(tables):
            rows = t.find_all("tr")
            print(f"  Table {i}: {len(rows)} rows, id={t.get('id','')!r}, class={t.get('class','')!r}")

        # Print first table with data rows
        for t in tables:
            data_rows = [r for r in t.find_all("tr") if r.find_all("td")]
            if data_rows:
                print(f"\n  First data table HTML ({len(data_rows)} data rows shown):")
                print(str(t)[:3000])
                break
        else:
            print("\n  No data table found — printing page body text:")
            print(await page.inner_text("body"))

        # ── 9. Check for error messages ───────────────────────────────────────
        print("\n--- Validation / error messages on page ---")
        body_text = await page.inner_text("body")
        for line in body_text.splitlines():
            line = line.strip()
            if line and any(kw in line.lower() for kw in ["error", "invalid", "required", "must", "please"]):
                print(f"  {line}")

        await browser.close()
        print("\n[Done]")


asyncio.run(main())
