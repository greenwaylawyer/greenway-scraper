"""Deduplication utilities.

Generates SHA256 fingerprints for lawyer records to detect duplicates.
Fingerprint based on: (first_name + last_name + bar_number + state)
"""

import hashlib
from typing import Optional
from scrapers.base import LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


class FingerprintGenerator:
    """Generate fingerprints for deduplication."""

    @staticmethod
    def generate(data: LawyerRawData | dict, state: str) -> str:
        """
        Generate SHA256 fingerprint for a lawyer record.

        Fingerprint is based on: (first_name + last_name + bar_number + state)
        This catches duplicates from different sources.

        Args:
            data: LawyerRawData object or dict with lawyer data
            state: 2-letter state code

        Returns:
            SHA256 hex digest (64 characters)
        """
        # Extract components
        if isinstance(data, LawyerRawData):
            bar_number = data.bar_number or ''
        else:
            bar_number = data.get('bar_number', '') or ''

        # Fingerprint is based only on (bar_number + state) so that records
        # can be reliably upserted even if the name changes between scrape runs.
        normalized = f"{bar_number} {state}".lower().strip()

        # Generate SHA256 hash
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()

        return fingerprint
