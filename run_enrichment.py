#!/usr/bin/env python3
"""
CLI entry point for Layer 3 & 4 enrichment workers.

Usage:
    python run_enrichment.py --worker discovery --source justia
    python run_enrichment.py --worker scraping --source justia
    python run_enrichment.py --worker layer4
    python run_enrichment.py --worker all
    
Options:
    --worker: Which worker to run (discovery, scraping, layer4, all)
    --source: Process only this source (optional, for discovery/scraping)
    --dry-run: Run without writing to database
    --limit: Maximum number of records to process (for --once mode)
    --once: Run one cycle and exit (default: continuous)
    --poll-interval: Seconds between poll cycles (default: 30)
    --batch-size: Number of requests per cycle (default: varies by worker)
"""

import asyncio
import sys
import argparse
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from workers.discovery_worker import DiscoveryWorker
from workers.scraping_worker import ScrapingWorker
from config.loader import get_enabled_sources
from utils.logger import get_logger

logger = get_logger(__name__)


async def run_discovery_worker(args):
    """Run discovery worker."""
    logger.info(
        "Starting discovery worker",
        source=args.source or "all",
        once=args.once,
        batch_size=args.batch_size or 20,
    )
    
    worker = DiscoveryWorker(
        source_key=args.source,
        batch_size=args.batch_size or 20,
    )
    
    if args.once:
        await worker.run_once()
        logger.info("Discovery worker completed one cycle", **worker.stats)
    else:
        await worker.run(poll_interval=args.poll_interval or 30)


async def run_scraping_worker(args):
    """Run scraping worker."""
    logger.info(
        "Starting scraping worker",
        source=args.source or "all",
        once=args.once,
        batch_size=args.batch_size or 10,
    )
    
    worker = ScrapingWorker(
        source_key=args.source,
        batch_size=args.batch_size or 10,
    )
    
    if args.once:
        await worker.run_once()
        logger.info("Scraping worker completed one cycle", **worker.stats)
    else:
        await worker.run(poll_interval=args.poll_interval or 30)


async def run_layer4_worker(args):
    """Run Layer 4 (Google Maps) worker."""
    logger.info(
        "Starting Layer 4 worker",
        once=args.once,
        batch_size=args.batch_size or 500,
    )
    
    # Layer 4 runs via the standard scraping worker against source_key=google_maps.
    # Discovery finds place_id; scraping performs place/details and merges results.
    worker = ScrapingWorker(
        source_key='google_maps',
        batch_size=args.batch_size or 10,
    )

    if args.once:
        await worker.run_once()
        logger.info("Layer 4 worker completed one cycle", **worker.stats)
    else:
        await worker.run(poll_interval=args.poll_interval or 30)


async def run_all_workers(args):
    """Run all workers in parallel."""
    logger.info("Starting all workers", once=args.once)
    
    # Get enabled sources
    enabled_layer3 = get_enabled_sources(layer=3)
    enabled_layer4 = get_enabled_sources(layer=4)
    
    if not enabled_layer3 and not enabled_layer4:
        logger.warning("No enabled sources found in config")
        return
    
    # Create worker tasks
    tasks = []
    
    # Discovery worker for each Layer 3 source
    for source_key in enabled_layer3:
        discovery_worker = DiscoveryWorker(
            source_key=source_key,
            batch_size=args.batch_size or 20,
        )
        if args.once:
            tasks.append(discovery_worker.run_once())
        else:
            tasks.append(discovery_worker.run(poll_interval=args.poll_interval or 30))
    
    # Scraping worker for each Layer 3 source
    for source_key in enabled_layer3:
        scraping_worker = ScrapingWorker(
            source_key=source_key,
            batch_size=args.batch_size or 10,
        )
        if args.once:
            tasks.append(scraping_worker.run_once())
        else:
            tasks.append(scraping_worker.run(poll_interval=args.poll_interval or 30))
    
    # Layer 4 worker (Google Maps)
    if enabled_layer4 and 'google_maps' in enabled_layer4:
        layer4_worker = ScrapingWorker(
            source_key='google_maps',
            batch_size=args.batch_size or 10,
        )
        if args.once:
            tasks.append(layer4_worker.run_once())
        else:
            tasks.append(layer4_worker.run(poll_interval=args.poll_interval or 30))
    
    if not tasks:
        logger.warning("No tasks created — check if sources are enabled in config")
        return
    
    # Run all tasks
    if args.once:
        await asyncio.gather(*tasks)
        logger.info("All workers completed one cycle")
    else:
        await asyncio.gather(*tasks)


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Layer 3 & 4 Enrichment Workers',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    
    parser.add_argument(
        '--worker',
        choices=['discovery', 'scraping', 'layer4', 'all'],
        required=True,
        help='Which worker to run',
    )
    
    parser.add_argument(
        '--source',
        help='Process only this source (e.g., justia, avvo)',
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Dry run mode (not yet implemented)',
    )
    
    parser.add_argument(
        '--limit',
        type=int,
        help='Maximum number of records to process (for --once mode)',
    )
    
    parser.add_argument(
        '--once',
        action='store_true',
        help='Run one poll cycle and exit (default: continuous)',
    )
    
    parser.add_argument(
        '--poll-interval',
        type=int,
        default=30,
        help='Seconds between poll cycles (default: 30)',
    )
    
    parser.add_argument(
        '--batch-size',
        type=int,
        help='Number of requests per cycle (default varies by worker)',
    )
    
    args = parser.parse_args()
    
    # Validate source argument
    if args.source and args.worker == 'layer4':
        logger.warning("--source is ignored for layer4 worker")
    
    # Dry run not yet implemented
    if args.dry_run:
        logger.error("--dry-run mode not yet implemented")
        sys.exit(1)
    
    # Route to appropriate worker
    try:
        if args.worker == 'discovery':
            asyncio.run(run_discovery_worker(args))
        elif args.worker == 'scraping':
            asyncio.run(run_scraping_worker(args))
        elif args.worker == 'layer4':
            asyncio.run(run_layer4_worker(args))
        elif args.worker == 'all':
            asyncio.run(run_all_workers(args))
    except KeyboardInterrupt:
        logger.info("Worker interrupted by user")
        sys.exit(0)
    except Exception as e:
        logger.error(f"Worker failed: {e}", exc_info=True)
        sys.exit(1)


if __name__ == '__main__':
    main()
