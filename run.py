#!/usr/bin/env python3
"""
Greenway Scraper - CLI entry point.

Usage:
    python run.py --state california --export-json
    python run.py --state california --export-scraper --layer state_bars
    python run.py --state california --export-json --export-scraper
    python run.py --all
"""

import argparse
import asyncio
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from scrapers.states.california import StateBarCalifornia
from scrapers.states.north_carolina import StateBarNorthCarolina
from scrapers.states.new_york import StateBarNewYork
from scrapers.base import LawyerRawData
from scrapers.enrichers.detail_scraper import DetailPageScraper, get_enricher_for_state
from pipeline.exporter import DataExporter
from utils.checkpoint import CheckpointManager
from utils.batch_tracker import BatchTracker
from utils.logger import get_logger

logger = get_logger(__name__)


# Mapping of state codes to scraper classes
SCRAPER_CLASSES = {
    'california': StateBarCalifornia,
    'north_carolina': StateBarNorthCarolina,
    'new_york': StateBarNewYork,
    # Add more states as they're implemented:
    # 'texas': StateBarTexas,
    # 'florida': StateBarFlorida,
}

# State code to abbreviation mapping
STATE_ABBREVIATIONS = {
    'california': 'CA',
    'north_carolina': 'NC',
    'new_york': 'NY',
    'texas': 'TX',
    'florida': 'FL',
    # Add more as needed
}

def get_state_abbr(state_code: str) -> str:
    """Get 2-letter state abbreviation from state code."""
    return STATE_ABBREVIATIONS.get(state_code, state_code[:2].upper())


def parse_args():
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(
        description='Greenway Scraper - Acquire lawyer profiles from state bar associations'
    )

    parser.add_argument(
        '--state',
        type=str,
        help='State code to scrape (e.g., california, new_york)',
    )

    parser.add_argument(
        '--all',
        action='store_true',
        help='Scrape all configured states',
    )

    parser.add_argument(
        '--export-json',
        action='store_true',
        help='Export results to JSON file (default: True)',
    )

    parser.add_argument(
        '--export-scraper',
        action='store_true',
        help='Export results to scraper database (requires DB config)',
    )

    parser.add_argument(
        '--layer',
        type=str,
        default='state_bars',
        help='Enrichment layer name (e.g., state_bars, justia, courtlistener)',
    )

    parser.add_argument(
        '--level',
        type=int,
        default=1,
        choices=[1, 2, 3, 4, 5],
        help='Enrichment level to run (1=list, 2=detail, 3=multi-source, 4=reviews, 5=future)',
    )

    parser.add_argument(
        '--auto-mode',
        action='store_true',
        help='Run all levels sequentially (1→2→3...)',
    )

    parser.add_argument(
        '--limit',
        type=int,
        help='Limit number of records to process (useful for testing)',
    )

    parser.add_argument(
        '--test',
        action='store_true',
        help='Test mode: limit to 5 records, disable checkpoint, enable JSON export (quick test run)',
    )

    parser.add_argument(
        '--start-page',
        type=int,
        default=1,
        help='Page number to start from (for resume capability)',
    )

    parser.add_argument(
        '--headless',
        type=lambda x: x.lower() == 'true',
        default=True,
        help='Run browser in headless mode (default: True)',
    )

    parser.add_argument(
        '--no-checkpoint',
        action='store_true',
        help='Disable checkpoint saving',
    )

    parser.add_argument(
        '--clear-checkpoint',
        type=str,
        metavar='STATE',
        help='Clear checkpoint for specified state before starting',
    )

    # AI mode flags — override config/ai.yaml at runtime
    ai_group = parser.add_mutually_exclusive_group()
    ai_group.add_argument(
        '--ai',
        action='store_true',
        dest='ai_enabled',
        default=None,
        help='Force-enable AI features (overrides config/ai.yaml)',
    )
    ai_group.add_argument(
        '--no-ai',
        action='store_false',
        dest='ai_enabled',
        help='Force-disable ALL AI features (overrides config/ai.yaml)',
    )

    return parser.parse_args()


async def scrape_state(
    state_code: str,
    export_json: bool = True,
    export_scraper: bool = False,
    layer_name: str = 'state_bars',
    start_page: int = 1,
    headless: bool = True,
    checkpoint_enabled: bool = True,
    limit: int = None,
) -> list[LawyerRawData]:
    """
    Scrape a single state.

    Args:
        state_code: State code (e.g., 'california')
        export_json: Whether to export to JSON
        export_scraper: Whether to export to scraper database
        layer_name: Enrichment layer name
        start_page: Page number to start from
        headless: Whether to run browser in headless mode
        checkpoint_enabled: Whether to enable checkpoint saving
        limit: Maximum number of records to scrape (None = no limit)

    Returns:
        List of scraped lawyer data
    """
    # Check if state is supported
    if state_code not in SCRAPER_CLASSES:
        logger.error(
            "State not supported",
            state=state_code,
            supported_states=list(SCRAPER_CLASSES.keys()),
        )
        return []

    # Clear checkpoint if requested
    if checkpoint_enabled and args.clear_checkpoint == state_code:
        checkpoint = CheckpointManager(state_code)
        checkpoint.clear()
        logger.info("Cleared checkpoint", state=state_code)

    # Initialize batch tracker if exporting to database
    batch_tracker = None
    if export_scraper:
        batch_tracker = BatchTracker(
            state=get_state_abbr(state_code),
            layer_name=layer_name,
        )
        # Create batch record with 'running' status
        batch_tracker.create_batch(
            total_records=0,  # Will be updated as we scrape
            metadata={
                'headless': headless,
                'start_page': start_page,
                'checkpoint_enabled': checkpoint_enabled,
            }
        )
        logger.info(f"Batch tracker initialized: {batch_tracker.batch_id}")

    # Initialize scraper
    scraper_class = SCRAPER_CLASSES[state_code]
    scraper = scraper_class(
        start_page=start_page,
        headless=headless,
        checkpoint_enabled=checkpoint_enabled,
        batch_tracker=batch_tracker,
        limit=limit,
    )

    # Run scraper
    logger.info(f"Starting scrape for {state_code}...")

    try:
        lawyers = await scraper.run()

        if not lawyers:
            logger.warning("No lawyers found", state=state_code)
            if batch_tracker:
                batch_tracker.complete_batch(status='completed')
                batch_tracker.close()
            return []

        # Print summary for test mode
        if limit and len(lawyers) <= 10:
            logger.info("=" * 60)
            logger.info("📋 SCRAPED LAWYERS SUMMARY")
            logger.info("=" * 60)
            for i, lawyer in enumerate(lawyers, 1):
                name = lawyer.full_name or "N/A"
                bar = lawyer.bar_number or "N/A"
                status = lawyer.license_status or "N/A"
                detail_url = lawyer.detail_url or "N/A"
                print(f"\n[{i}] {name}")
                print(f"    Bar #: {bar}")
                print(f"    Status: {status}")
                print(f"    Profile: {detail_url}")
            logger.info("=" * 60)

        # Export results
        exporter = DataExporter(
            state=get_state_abbr(state_code),  # First 2 chars as state code
            source=scraper.SOURCE,
            batch_id=batch_tracker.batch_id if batch_tracker else None,
        )

        if export_json:
            output_path = exporter.export_json(lawyers)
            logger.info(f"Exported to: {output_path}")

        if export_scraper:
            try:
                count = exporter.export_to_database(lawyers, layer_name=layer_name)
                logger.info(
                    "Database export complete",
                    state=state_code,
                    layer=layer_name,
                    records=count,
                )

                # Mark batch as completed
                if batch_tracker:
                    batch_tracker.complete_batch(status='completed')

            except Exception as e:
                logger.error(
                    "Database export failed",
                    state=state_code,
                    layer=layer_name,
                    error=str(e),
                )

                # Mark batch as failed
                if batch_tracker:
                    batch_tracker.log_error(
                        error_message=str(e),
                        error_type=type(e).__name__,
                    )
                    batch_tracker.complete_batch(status='failed', error_log=str(e))

                # Continue even if DB export fails

        return lawyers

    except Exception as e:
        logger.error(f"Scraping failed for {state_code}", error=str(e))

        # Log error to enrichment_errors and mark batch as failed
        if batch_tracker:
            import traceback
            batch_tracker.log_error(
                error_message=str(e),
                error_type=type(e).__name__,
                error_stacktrace=traceback.format_exc(),
            )
            batch_tracker.complete_batch(status='failed', error_log=str(e))

        raise

    finally:
        # Ensure batch tracker is closed
        if batch_tracker:
            batch_tracker.close()


async def scrape_detail_pages(
    state_code: str,
    export_json: bool = True,
    export_scraper: bool = False,
    layer_name: str = 'calbar_details',
    headless: bool = True,
    limit: int = None,
    source_level: int = 1,
    target_level: int = 2,
) -> list[LawyerRawData]:
    """
    Scrape detail pages for lawyers (Level 2 enrichment).

    Args:
        state_code: State code (e.g., 'california')
        export_json: Whether to export to JSON
        export_scraper: Whether to export to scraper database
        layer_name: Enrichment layer name (e.g., 'calbar_details')
        headless: Whether to run browser in headless mode
        limit: Maximum number of lawyers to process
        source_level: Source enrichment level to query (default: 1)
        target_level: Target enrichment level after completion (default: 2)

    Returns:
        List of enriched lawyer data
    """
    state_abbr = get_state_abbr(state_code)

    # Get the appropriate enricher for this state
    enricher = get_enricher_for_state(state_abbr)
    if not enricher:
        logger.error(f"No detail enricher available for state: {state_abbr}")
        return []

    # Initialize batch tracker if exporting to database
    batch_tracker = None
    if export_scraper:
        batch_tracker = BatchTracker(
            state=state_abbr,
            layer_name=layer_name,
        )
        # Create batch record with level 2
        batch_tracker.create_batch(
            total_records=0,  # Will be updated as we scrape
            enrichment_level=target_level,
            metadata={
                'headless': headless,
                'source_level': source_level,
                'target_level': target_level,
                'limit': limit,
            }
        )
        logger.info(f"Batch tracker initialized for level {target_level}: {batch_tracker.batch_id}")

    # Initialize detail scraper
    scraper = DetailPageScraper(
        state_code=state_abbr,
        enricher=enricher,
        source_level=source_level,
        target_level=target_level,
        headless=headless,
        batch_tracker=batch_tracker,
        limit=limit,
    )

    # Run detail scraping
    logger.info(f"Starting level {target_level} enrichment for {state_code}...")

    try:
        lawyers = await scraper.run()

        if not lawyers:
            logger.warning("No lawyers enriched", state=state_code, level=target_level)
            if batch_tracker:
                batch_tracker.complete_batch(status='completed')
                batch_tracker.close()
            return []

        # Export results
        exporter = DataExporter(
            state=state_abbr,
            source=enricher.SOURCE,
            batch_id=batch_tracker.batch_id if batch_tracker else None,
        )

        if export_json:
            output_path = exporter.export_json(lawyers)
            logger.info(f"Exported to: {output_path}")

        if export_scraper:
            try:
                count = exporter.export_to_database(
                    lawyers,
                    layer_name=layer_name,
                    enrichment_level=target_level,
                )
                logger.info(
                    "Database export complete",
                    state=state_code,
                    layer=layer_name,
                    level=target_level,
                    records=count,
                )

                # Mark batch as completed
                if batch_tracker:
                    batch_tracker.complete_batch(status='completed')

            except Exception as e:
                logger.error(
                    "Database export failed",
                    state=state_code,
                    layer=layer_name,
                    level=target_level,
                    error=str(e),
                )

                # Mark batch as failed
                if batch_tracker:
                    batch_tracker.log_error(
                        error_message=str(e),
                        error_type=type(e).__name__,
                    )
                    batch_tracker.complete_batch(status='failed', error_log=str(e))

        return lawyers

    except Exception as e:
        logger.error(f"Level {target_level} enrichment failed for {state_code}", error=str(e))

        # Log error to enrichment_errors and mark batch as failed
        if batch_tracker:
            import traceback
            batch_tracker.log_error(
                error_message=str(e),
                error_type=type(e).__name__,
                error_stacktrace=traceback.format_exc(),
            )
            batch_tracker.complete_batch(status='failed', error_log=str(e))

        raise

    finally:
        # Ensure batch tracker is closed
        if batch_tracker:
            batch_tracker.close()


async def run_auto_mode(
    state_code: str,
    export_json: bool = True,
    export_scraper: bool = False,
    headless: bool = True,
    checkpoint_enabled: bool = True,
    limit: int = None,
) -> dict:
    """
    Run auto-mode: execute levels 1→2→3... sequentially.

    Args:
        state_code: State code (e.g., 'california')
        export_json: Whether to export to JSON
        export_scraper: Whether to export to scraper database
        headless: Whether to run browser in headless mode
        checkpoint_enabled: Whether to enable checkpoint saving
        limit: Maximum number of records to process per level

    Returns:
        Dictionary with results for each level
    """
    logger.info(f"🚀 Starting auto-mode for {state_code}")

    results = {}

    # Level 1: List scraping
    logger.info("=" * 60)
    logger.info("📋 Level 1: List Scraping")
    logger.info("=" * 60)
    try:
        level_1_lawyers = await scrape_state(
            state_code=state_code,
            export_json=export_json,
            export_scraper=export_scraper,
            layer_name='state_bars',
            start_page=1,
            headless=headless,
            checkpoint_enabled=checkpoint_enabled,
            limit=limit,
        )
        results['level_1'] = {
            'status': 'completed',
            'count': len(level_1_lawyers),
        }
        logger.info(f"✅ Level 1 completed: {len(level_1_lawyers)} lawyers")
    except Exception as e:
        logger.error(f"❌ Level 1 failed: {str(e)}")
        results['level_1'] = {'status': 'failed', 'error': str(e)}
        return results  # Stop if level 1 fails

    # Level 2: Detail page scraping
    logger.info("=" * 60)
    logger.info("📄 Level 2: Detail Page Scraping")
    logger.info("=" * 60)
    state_abbr = get_state_abbr(state_code)
    _enricher = get_enricher_for_state(state_abbr)
    detail_layer_name = _enricher.SOURCE if _enricher else f'{state_abbr.lower()}bar_details'
    try:
        level_2_lawyers = await scrape_detail_pages(
            state_code=state_code,
            export_json=export_json,
            export_scraper=export_scraper,
            layer_name=detail_layer_name,
            headless=headless,
            limit=limit,
            source_level=1,
            target_level=2,
        )
        results['level_2'] = {
            'status': 'completed',
            'count': len(level_2_lawyers),
        }
        logger.info(f"✅ Level 2 completed: {len(level_2_lawyers)} lawyers enriched")
    except Exception as e:
        logger.error(f"❌ Level 2 failed: {str(e)}")
        results['level_2'] = {'status': 'failed', 'error': str(e)}
        # Continue even if level 2 fails

    # Level 3, 4, 5 can be added here in future implementations
    logger.info("=" * 60)
    logger.info(f"🎉 Auto-mode completed for {state_code}")
    logger.info("=" * 60)
    logger.info(f"Results: {results}")

    return results


async def main():
    """Main entry point."""
    global args
    args = parse_args()

    # Handle test mode - apply test-friendly defaults
    if args.test:
        logger.info("🧪 TEST MODE ENABLED - Limiting to 5 records, checkpoint disabled")
        if args.limit is None:
            args.limit = 5
        args.no_checkpoint = True
        if not args.export_json and not args.export_scraper:
            args.export_json = True  # Default to JSON in test mode

    # Apply AI mode override (--ai / --no-ai flags take precedence over config/ai.yaml)
    if args.ai_enabled is not None:
        try:
            from ai import config as ai_config
            ai_config._load()           # ensure config is loaded
            ai_config._CONFIG['ai']['enabled'] = args.ai_enabled
            logger.info("AI mode overridden by CLI", ai_enabled=args.ai_enabled)
        except Exception as e:
            logger.warning("Could not apply AI mode override", error=str(e))

    # Validate arguments
    if not args.state and not args.all:
        logger.error("Must specify --state or --all")
        sys.exit(1)

    checkpoint_enabled = not args.no_checkpoint

    # Auto-mode: run all levels sequentially
    if args.auto_mode:
        if not args.state:
            logger.error("Auto-mode requires --state argument")
            sys.exit(1)

        results = await run_auto_mode(
            state_code=args.state,
            export_json=args.export_json,
            export_scraper=args.export_scraper,
            headless=args.headless,
            checkpoint_enabled=checkpoint_enabled,
            limit=args.limit,
        )
        logger.info(f"Auto-mode results: {results}")
        sys.exit(0)

    # Manual level execution
    if args.level == 2:
        # Level 2: Detail page scraping
        _state_abbr = get_state_abbr(args.state)
        _enricher = get_enricher_for_state(_state_abbr)
        _detail_layer = _enricher.SOURCE if _enricher else f'{_state_abbr.lower()}bar_details'
        lawyers = await scrape_detail_pages(
            state_code=args.state,
            export_json=args.export_json,
            export_scraper=args.export_scraper,
            layer_name=_detail_layer,
            headless=args.headless,
            limit=args.limit,
            source_level=1,
            target_level=2,
        )
        logger.info(f"Level 2 complete: Enriched {len(lawyers)} lawyers")
        sys.exit(0)

    # Level 1: List scraping (default)
    if args.state:
        lawyers = await scrape_state(
            state_code=args.state,
            export_json=args.export_json,
            export_scraper=args.export_scraper,
            layer_name=args.layer,
            start_page=args.start_page,
            headless=args.headless,
            checkpoint_enabled=checkpoint_enabled,
            limit=args.limit,
        )
        logger.info(f"Scraped {len(lawyers)} lawyers from {args.state}")
        sys.exit(0)

    # Scrape all states
    if args.all:
        all_lawyers = []
        for state_code in SCRAPER_CLASSES.keys():
            logger.info(f"Scraping {state_code}...")
            lawyers = await scrape_state(
                state_code=state_code,
                export_json=args.export_json,
                export_scraper=args.export_scraper,
                layer_name=args.layer,
                start_page=args.start_page,
                headless=args.headless,
                checkpoint_enabled=checkpoint_enabled,
                limit=args.limit,
            )
            all_lawyers.extend(lawyers)

        logger.info(f"Total lawyers scraped: {len(all_lawyers)}")
        sys.exit(0)


if __name__ == '__main__':
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Scraping interrupted by user")
        sys.exit(130)
    except Exception as e:
        logger.error("Scraping failed", error=str(e))
        sys.exit(1)
