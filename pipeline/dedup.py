"""Deduplication utilities.

Generates SHA256 fingerprints for lawyer records to detect duplicates.
Fingerprint based on: (normalized bar_number + state)
"""

import hashlib
from typing import Optional
from scrapers.base import LawyerRawData
from utils.logger import get_logger

logger = get_logger(__name__)


def normalize_bar_number(bar_number: Optional[str]) -> str:
    """
    Canonical form for identity matching and fingerprints.

    Strips whitespace, removes a leading '#', collapses internal whitespace,
    case-folds. Does not strip leading zeros (some states use them).
    """
    if bar_number is None:
        return ""
    s = str(bar_number).strip()
    if s.startswith("#"):
        s = s[1:].strip()
    s = " ".join(s.split())
    return s.casefold()


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
        if isinstance(data, LawyerRawData):
            raw_bar = data.bar_number
        else:
            raw_bar = data.get("bar_number")

        bar_key = normalize_bar_number(raw_bar)

        # Fingerprint is (normalized bar_number + state) so listing vs detail
        # rows with the same license number merge into one row.
        state_key = (state or "").strip().casefold()
        normalized = f"{bar_key} {state_key}".strip()

        # Generate SHA256 hash
        fingerprint = hashlib.sha256(normalized.encode()).hexdigest()

        return fingerprint
