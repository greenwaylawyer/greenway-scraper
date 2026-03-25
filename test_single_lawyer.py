#!/usr/bin/env python3
"""
Test script to scrape a single lawyer's detail page with verbose logging.

Usage:
    python test_single_lawyer.py <bar_number> [--visible]
    python test_single_lawyer.py --from-db [bar_number] [--visible]

Examples:
    python test_single_lawyer.py 123456
    python test_single_lawyer.py 123456 --visible
    python test_single_lawyer.py --from-db
    python test_single_lawyer.py --from-db 123456

Note: Runs in headless mode by default (Docker-compatible).
      Use --visible flag only if you have X server (for local testing).
"""

import asyncio
import sys
import os
import json
from dataclasses import asdict
from dotenv import load_dotenv
from playwright.async_api import async_playwright
from scrapers.enrichers.calbar_details import CaliforniaDetailEnricher
from scrapers.base import LawyerRawData
from utils.logger import get_logger

# Load environment variables
load_dotenv(override=True)

# Set up verbose logging
logger = get_logger(__name__)


async def test_single_lawyer(bar_number: str, headless: bool = True):
    """Test scraping a single lawyer's detail page."""

    print("=" * 80)
    print(f"TESTING DETAIL PAGE SCRAPING FOR BAR NUMBER: {bar_number}")
    print("=" * 80)

    # Initialize enricher
    enricher = CaliforniaDetailEnricher()
    detail_url = enricher.construct_detail_url(bar_number)

    print(f"\n📍 Detail URL: {detail_url}")
    print(f"🔍 Starting browser (headless={headless})...")

    # Launch browser
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=headless)
        context = await browser.new_context(
            viewport={'width': 1920, 'height': 1080},
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36'
        )
        page = await context.new_page()

        try:
            print(f"🌐 Navigating to detail page...")
            await page.goto(detail_url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(2000)  # Wait for dynamic content

            print(f"✅ Page loaded successfully")
            print(f"\n📄 Page title: {await page.title()}")

            # Dump the main content HTML for debugging
            print("\n" + "=" * 80)
            print("PAGE STRUCTURE (main content)")
            print("=" * 80)
            try:
                all_html = await page.content()
                # Find the bar number in HTML (skip meta tags)
                search_str = f'#108715'
                idx = all_html.find(search_str)
                if idx < 0:
                    search_str = f'108715'
                    # Find second occurrence (skip meta tags)
                    first = all_html.find(search_str)
                    idx = all_html.find(search_str, first + 1)
                if idx >= 0:
                    start = max(0, idx - 300)
                    print(all_html[start:start+5000])
                else:
                    # Try to find by Address
                    idx = all_html.find('Address:')
                    if idx >= 0:
                        start = max(0, idx - 500)
                        print(all_html[start:start+5000])
                    else:
                        print("Could not find profile content in HTML")
                        print(all_html[:3000])
            except Exception as e:
                print(f"Could not dump HTML: {e}")

            # Create a simple existing data object
            existing_data = LawyerRawData(
                full_name="Test Lawyer",
                bar_number=bar_number,
                license_status="Active"
            )

            print(f"\n🔧 Parsing detail page...")

            # Parse the detail page
            enriched_data = await enricher.parse_detail_page(
                page,
                bar_number,
                existing_data
            )

            print("\n" + "=" * 80)
            print("ENRICHED DATA RESULTS")
            print("=" * 80)

            # Display all extracted data
            data_dict = asdict(enriched_data)
            for key, value in data_dict.items():
                if value is not None:
                    if isinstance(value, list):
                        print(f"\n{key}:")
                        for item in value:
                            print(f"  - {item}")
                    else:
                        print(f"\n{key}: {value}")

            # Also show as JSON
            print("\n" + "=" * 80)
            print("JSON OUTPUT")
            print("=" * 80)
            print(json.dumps(data_dict, indent=2, default=str))

            # Calculate completeness
            filled_fields = sum(1 for v in data_dict.values() if v is not None and v != '' and v != [])
            total_fields = len(data_dict)
            completeness = (filled_fields / total_fields) * 100

            print("\n" + "=" * 80)
            print("STATISTICS")
            print("=" * 80)
            print(f"Filled fields: {filled_fields}/{total_fields}")
            print(f"Completeness: {completeness:.1f}%")
            print(f"Detail URL: {enriched_data.detail_url}")

            # Take a screenshot for debugging
            screenshot_path = f"output/test_lawyer_{bar_number}.png"
            os.makedirs("output", exist_ok=True)
            await page.screenshot(path=screenshot_path, full_page=True)
            print(f"\n📸 Screenshot saved to: {screenshot_path}")

        except Exception as e:
            print(f"\n❌ ERROR: {type(e).__name__}: {str(e)}")
            import traceback
            traceback.print_exc()

            # Take error screenshot
            try:
                screenshot_path = f"output/error_lawyer_{bar_number}.png"
                await page.screenshot(path=screenshot_path, full_page=True)
                print(f"\n📸 Error screenshot saved to: {screenshot_path}")
            except:
                pass

        finally:
            await browser.close()
            print("\n✅ Browser closed")


async def test_from_database(bar_number: str = None, limit: int = 1, headless: bool = True):
    """Test by fetching a lawyer from the database."""
    import psycopg2
    from psycopg2.extras import RealDictCursor

    print("=" * 80)
    print("TESTING WITH DATABASE LAWYER")
    print("=" * 80)

    # Connect to database
    conn = psycopg2.connect(
        host=os.getenv('SCRAPER_DB_HOST', '127.0.0.1'),
        port=int(os.getenv('SCRAPER_DB_PORT', 5433)),
        database=os.getenv('SCRAPER_DB_NAME', 'greenway_scraper'),
        user=os.getenv('SCRAPER_DB_USER', 'scraper'),
        password=os.getenv('SCRAPER_DB_PASSWORD', 'scraper_secret'),
    )

    try:
        with conn.cursor(cursor_factory=RealDictCursor) as cursor:
            if bar_number:
                query = """
                    SELECT bar_number, full_name, enrichment_level, merged_data
                    FROM lawyer_enrichment
                    WHERE bar_number = %s AND license_state = 'CA'
                    LIMIT 1
                """
                cursor.execute(query, (bar_number,))
            else:
                query = """
                    SELECT bar_number, full_name, enrichment_level, merged_data
                    FROM lawyer_enrichment
                    WHERE license_state = 'CA' AND enrichment_level = 1
                    AND bar_number IS NOT NULL
                    ORDER BY RANDOM()
                    LIMIT %s
                """
                cursor.execute(query, (limit,))

            lawyer = cursor.fetchone()

            if not lawyer:
                print(f"❌ No lawyer found in database")
                if bar_number:
                    print(f"   Bar number: {bar_number}")
                return

            print(f"\n📋 Found lawyer in database:")
            print(f"   Bar Number: {lawyer['bar_number']}")
            print(f"   Name: {lawyer['full_name']}")
            print(f"   Current Level: {lawyer['enrichment_level']}")

            # Test scraping this lawyer
            await test_single_lawyer(lawyer['bar_number'], headless=headless)

    finally:
        conn.close()


def main():
    """Main entry point."""
    if len(sys.argv) < 2:
        print("Usage: python test_single_lawyer.py <bar_number> [--visible]")
        print("   or: python test_single_lawyer.py --from-db [bar_number] [--visible]")
        print("\nExamples:")
        print("  python test_single_lawyer.py 123456")
        print("  python test_single_lawyer.py 123456 --visible    # Show browser (requires X server)")
        print("  python test_single_lawyer.py --from-db           # Random lawyer from DB")
        print("  python test_single_lawyer.py --from-db 123456    # Specific lawyer from DB")
        print("\nNote: By default runs in headless mode (Docker-compatible).")
        print("      Use --visible flag only if you have X server (e.g., running locally).")
        sys.exit(1)

    # Check for --visible flag
    headless = '--visible' not in sys.argv

    if sys.argv[1] == '--from-db':
        # Test with database lawyer
        bar_number = sys.argv[2] if len(sys.argv) > 2 and sys.argv[2] != '--visible' else None
        asyncio.run(test_from_database(bar_number=bar_number, headless=headless))
    else:
        # Test with provided bar number
        bar_number = sys.argv[1]
        asyncio.run(test_single_lawyer(bar_number, headless=headless))


if __name__ == "__main__":
    main()
