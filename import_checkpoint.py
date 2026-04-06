#!/usr/bin/env python3
"""
Import lawyers from checkpoint file to database.

Usage:
    python import_checkpoint.py --state north_carolina --export-scraper
    python import_checkpoint.py --state north_carolina --export-json
"""

import argparse
import json
import sys
from pathlib import Path

# Add project root to path
sys.path.insert(0, str(Path(__file__).parent))

from scrapers.base import LawyerRawData
from pipeline.exporter import DataExporter
from utils.logger import get_logger

logger = get_logger(__name__)

CHECKPOINT_DIR = Path(__file__).parent / "checkpoints"


def load_checkpoint(state_code: str) -> dict:
    """Load checkpoint file for a state."""
    checkpoint_path = CHECKPOINT_DIR / f"{state_code}.json"

    if not checkpoint_path.exists():
        raise FileNotFoundError(f"No checkpoint found for {state_code} at {checkpoint_path}")

    logger.info(f"Loading checkpoint from: {checkpoint_path}")
    with open(checkpoint_path, 'r') as f:
        return json.load(f)


def extract_lawyers_from_checkpoint(checkpoint_data: dict) -> list[LawyerRawData]:
    """
    Extract LawyerRawData objects from checkpoint data.

    Handles both checkpoint formats:
    1. New format: {"last_page": {"lawyers": [...]}}
    2. Old format: {"lawyers": [...]}
    """
    lawyers = []

    # Check for new format with last_page.lawyers
    if "last_page" in checkpoint_data and "lawyers" in checkpoint_data.get("last_page", {}):
        raw_lawyers = checkpoint_data["last_page"]["lawyers"]
        logger.info(f"Found {len(raw_lawyers)} lawyers in checkpoint (new format)")
    elif "lawyers" in checkpoint_data:
        raw_lawyers = checkpoint_data["lawyers"]
        logger.info(f"Found {len(raw_lawyers)} lawyers in checkpoint (old format)")
    else:
        logger.error("No lawyers found in checkpoint")
        return []

    # Convert dict records to LawyerRawData objects
    for raw in raw_lawyers:
        try:
            lawyer = LawyerRawData(
                full_name=raw.get("full_name"),
                bar_number=raw.get("bar_number"),
                firm_name=raw.get("firm_name"),
                address=raw.get("address"),
                phone=raw.get("phone"),
                fax=raw.get("fax"),
                email=raw.get("email"),
                website_url=raw.get("website_url"),
                practice_areas=raw.get("practice_areas"),
                bio=raw.get("bio"),
                photo_url=raw.get("photo_url"),
                law_school=raw.get("law_school"),
                admission_year=raw.get("admission_year"),
                license_status=raw.get("license_status"),
                detail_url=raw.get("detail_url"),
                raw_html=raw.get("raw_html"),
            )
            lawyers.append(lawyer)
        except Exception as e:
            logger.warning(f"Failed to parse lawyer record: {e}")
            continue

    logger.info(f"Successfully parsed {len(lawyers)} LawyerRawData objects")
    return lawyers


def main():
    parser = argparse.ArgumentParser(
        description="Import checkpoint data to database or JSON"
    )
    parser.add_argument(
        "--state",
        type=str,
        required=True,
        help="State code (e.g., north_carolina)",
    )
    parser.add_argument(
        "--export-json",
        action="store_true",
        help="Export to JSON file",
    )
    parser.add_argument(
        "--export-scraper",
        action="store_true",
        help="Export to scraper database",
    )
    parser.add_argument(
        "--layer",
        type=str,
        default="state_bars",
        help="Enrichment layer name (default: state_bars)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        help="Limit number of records to import (for testing)",
    )

    args = parser.parse_args()

    # Default to JSON export if nothing specified
    if not args.export_json and not args.export_scraper:
        args.export_json = True

    # Load checkpoint
    try:
        checkpoint_data = load_checkpoint(args.state)
    except FileNotFoundError as e:
        logger.error(str(e))
        sys.exit(1)

    # Extract lawyers
    lawyers = extract_lawyers_from_checkpoint(checkpoint_data)

    if not lawyers:
        logger.error("No lawyers to export")
        sys.exit(1)

    # Apply limit if specified
    if args.limit:
        lawyers = lawyers[:args.limit]
        logger.info(f"Limited to {len(lawyers)} lawyers")

    # Determine source and state abbreviation based on state code
    state_config = {
        "north_carolina": {"abbr": "NC", "source": "ncbar"},
        "california": {"abbr": "CA", "source": "calbar"},
    }

    config = state_config.get(args.state, {"abbr": args.state[:2].upper(), "source": args.state})
    state_abbr = config["abbr"]
    source = config["source"]

    # Initialize exporter
    exporter = DataExporter(
        state=state_abbr,
        source=source,
    )

    # Export to JSON
    if args.export_json:
        output_path = exporter.export_json(lawyers)
        logger.info(f"Exported to JSON: {output_path}")

    # Export to database
    if args.export_scraper:
        try:
            count = exporter.export_to_database(lawyers, layer_name=args.layer)
            logger.info(f"Exported {count} records to database")
        except Exception as e:
            logger.error(f"Database export failed: {e}")
            sys.exit(1)

    logger.info("Done!")


if __name__ == "__main__":
    main()
