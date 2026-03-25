"""Name normalization utilities.

Parses full names into first name, last name, and middle name/initial.
Handles suffixes like Jr., Sr., III, etc.
"""

import re
from typing import Optional, NamedTuple
from utils.logger import get_logger

logger = get_logger(__name__)


class ParsedName(NamedTuple):
    """Parsed name components."""
    first_name: Optional[str]
    last_name: Optional[str]
    middle_name: Optional[str] = None
    suffix: Optional[str] = None


class NameNormalizer:
    """Parse and normalize lawyer names."""

    # Common name suffixes
    SUFFIXES = [
        'jr', 'sr', 'ii', 'iii', 'iv', 'v', 'vi',
        'jr.', 'sr.', 'ii', 'iii', 'iv', 'v', 'vi',
        'esq', 'esquire', 'md', 'phd', 'jd', 'llm',
    ]

    # Suffix pattern (looks for suffix at end)
    SUFFIX_PATTERN = re.compile(
        r'\s+(?:' + '|'.join(re.escape(s) for s in SUFFIXES) + r')\.?\s*$',
        re.IGNORECASE
    )

    # Prefix pattern (looks for prefix at beginning)
    PREFIXES = ['mr', 'mrs', 'ms', 'dr', 'prof', 'hon', 'judge']
    PREFIX_PATTERN = re.compile(
        r'^(?:' + '|'.join(re.escape(p) for p in PREFIXES) + r')\.?\s+',
        re.IGNORECASE
    )

    @staticmethod
    def parse(full_name: Optional[str]) -> Optional[ParsedName]:
        """
        Parse a full name into components.

        Args:
            full_name: Full name string (e.g., "John A. Smith Jr.")

        Returns:
            ParsedName with first, last, middle, and suffix
            or None if name cannot be parsed
        """
        if not full_name or not full_name.strip():
            return None

        name = full_name.strip()

        # Detect "Last, First [Middle]" format (e.g. CA Bar list: "Zafar, Farah")
        # Only treat as Last, First if there's exactly one comma and the part after
        # it looks like a given name (not an address fragment like "Suite 100")
        if ',' in name:
            comma_parts = [p.strip() for p in name.split(',', 1)]
            after_comma = comma_parts[1]
            # Heuristic: after-comma portion is a name if it has no digits and
            # no address keywords
            _address_keywords = {'suite', 'ste', 'floor', 'fl', 'apt', 'blvd',
                                  'ave', 'st', 'rd', 'dr', '#'}
            after_lower_words = set(after_comma.lower().split())
            is_name_format = (
                not any(c.isdigit() for c in after_comma)
                and not (after_lower_words & _address_keywords)
            )
            if is_name_format:
                # Reconstruct as "First Last" for consistent parsing
                name = f"{after_comma} {comma_parts[0]}"

        # Remove prefix
        name = NameNormalizer.PREFIX_PATTERN.sub('', name)

        # Extract suffix
        suffix = None
        suffix_match = NameNormalizer.SUFFIX_PATTERN.search(name)
        if suffix_match:
            suffix = suffix_match.group(0).strip().rstrip('.')
            name = name[:suffix_match.start()].strip()

        # Split into parts
        parts = name.split()

        if len(parts) < 2:
            # Not enough parts for first + last
            logger.warning("Could not parse name", name=full_name)
            return ParsedName(
                first_name=name,
                last_name=None,
            )

        # Last name is always the last part
        last_name = parts[-1]

        # First name is the first part
        first_name = parts[0]

        # Everything in between is middle name/initial
        middle_name = None
        if len(parts) > 2:
            middle_name = ' '.join(parts[1:-1])

        return ParsedName(
            first_name=first_name,
            last_name=last_name,
            middle_name=middle_name,
            suffix=suffix,
        )

    @staticmethod
    def normalize(full_name: Optional[str]) -> dict:
        """
        Normalize a full name and return dictionary.

        Args:
            full_name: Raw full name string

        Returns:
            Dictionary with:
            - full_name: Cleaned full name
            - first_name: First name
            - last_name: Last name
            - middle_name: Middle name/initial (if present)
            - suffix: Name suffix (if present)
        """
        if not full_name or not full_name.strip():
            return {
                'full_name': None,
                'first_name': None,
                'last_name': None,
            }

        # Clean up name
        clean_name = ' '.join(full_name.split())

        parsed = NameNormalizer.parse(clean_name)

        # If input was "Last, First" format, store the normalised "First Last" form
        normalised_full = (
            f"{parsed.first_name} {parsed.last_name}"
            if parsed and parsed.first_name and parsed.last_name
            else clean_name
        )

        return {
            'full_name': normalised_full,
            'first_name': parsed.first_name if parsed else None,
            'last_name': parsed.last_name if parsed else None,
            'middle_name': parsed.middle_name if parsed and parsed.middle_name else None,
            'suffix': parsed.suffix if parsed and parsed.suffix else None,
        }
