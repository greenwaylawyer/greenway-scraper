"""Address normalization utilities.

Converts addresses to USPS standard format:
- Street number and name
- Secondary unit designators (Suite, Apt, Unit, etc.)
- City, State ZIP format
- Proper abbreviation of street types (St, Ave, Blvd, etc.)
"""

import re
from typing import Optional
from utils.logger import get_logger

logger = get_logger(__name__)


class AddressNormalizer:
    """Normalize addresses to USPS standard format."""

    # USPS street type abbreviations
    STREET_ABBREVIATIONS = {
        'street': 'st',
        'avenue': 'ave',
        'boulevard': 'blvd',
        'road': 'rd',
        'lane': 'ln',
        'drive': 'dr',
        'court': 'ct',
        'place': 'pl',
        'square': 'sq',
        'trail': 'trl',
        'parkway': 'pkwy',
        'circle': 'cir',
        'way': 'way',
        'terrace': 'ter',
    }

    # USPS secondary unit designators
    UNIT_ABBREVIATIONS = {
        'apartment': 'apt',
        'suite': 'ste',
        'unit': 'unit',
        'room': 'rm',
        'floor': 'fl',
        'building': 'bldg',
    }

    # State abbreviations (full name to 2-letter)
    STATE_ABBREVIATIONS = {
        'alabama': 'AL', 'alaska': 'AK', 'arizona': 'AZ', 'arkansas': 'AR',
        'california': 'CA', 'colorado': 'CO', 'connecticut': 'CT', 'delaware': 'DE',
        'florida': 'FL', 'georgia': 'GA', 'hawaii': 'HI', 'idaho': 'ID',
        'illinois': 'IL', 'indiana': 'IN', 'iowa': 'IA', 'kansas': 'KS',
        'kentucky': 'KY', 'louisiana': 'LA', 'maine': 'ME', 'maryland': 'MD',
        'massachusetts': 'MA', 'michigan': 'MI', 'minnesota': 'MN', 'mississippi': 'MS',
        'missouri': 'MO', 'montana': 'MT', 'nebraska': 'NE', 'nevada': 'NV',
        'new hampshire': 'NH', 'new jersey': 'NJ', 'new mexico': 'NM', 'new york': 'NY',
        'north carolina': 'NC', 'north dakota': 'ND', 'ohio': 'OH', 'oklahoma': 'OK',
        'oregon': 'OR', 'pennsylvania': 'PA', 'rhode island': 'RI', 'south carolina': 'SC',
        'south dakota': 'SD', 'tennessee': 'TN', 'texas': 'TX', 'utah': 'UT',
        'vermont': 'VT', 'virginia': 'VA', 'washington': 'WA', 'west virginia': 'WV',
        'wisconsin': 'WI', 'wyoming': 'WY',
        'district of columbia': 'DC',
    }

    @staticmethod
    def normalize(address: Optional[str]) -> Optional[dict]:
        """
        Normalize an address to USPS standard format.

        Args:
            address: Raw address string

        Returns:
            Dictionary with normalized components:
            - address_line1: Street address
            - address_line2: Unit/suite info
            - city: City name
            - state: 2-letter state code
            - zip: ZIP code
            - or None if address is invalid/empty
        """
        if not address or not address.strip():
            return None

        # Clean up the address
        address = re.sub(r'\s+', ' ', address.strip())
        address = re.sub(r',\s*', ', ', address)

        # Try to parse city, state, ZIP
        city = None
        state = None
        zip_code = None

        # Look for pattern: City, ST ZIP
        csz_pattern = r',\s*([A-Za-z\s]+?),?\s+([A-Za-z]{2})\s+(\d{5}(?:-\d{4})?)?$'
        match = re.search(csz_pattern, address, re.IGNORECASE)

        if match:
            city = match.group(1).strip().title()
            state_abbr = match.group(2).upper()
            zip_code = match.group(3) if match.group(3) else None

            # Normalize state abbreviation if full name was provided
            state_lower = state_abbr.lower()
            if state_lower in AddressNormalizer.STATE_ABBREVIATIONS:
                state = AddressNormalizer.STATE_ABBREVIATIONS[state_lower]
            else:
                # Assume it's already a 2-letter code
                state = state_abbr.upper()

            # Remove CSZ from address line
            address_lines = address[:match.start()].strip()
        else:
            address_lines = address

        # Split address lines
        lines = [line.strip() for line in address_lines.split(',') if line.strip()]

        address_line1 = lines[0] if lines else None
        address_line2 = lines[1] if len(lines) > 1 else None

        # Normalize street types
        if address_line1:
            address_line1 = AddressNormalizer._normalize_street_types(address_line1)

        result = {
            'address_line1': address_line1,
            'address_line2': address_line2,
            'city': city,
            'state': state,
            'zip': zip_code,
        }

        # Only return if we have at least some data
        if any(result.values()):
            return result

        return None

    @staticmethod
    def _normalize_street_types(address: str) -> str:
        """Abbreviate common street types to USPS standard."""
        words = address.split()
        normalized = []

        for word in words:
            lower = word.lower()
            if lower in AddressNormalizer.STREET_ABBREVIATIONS:
                normalized.append(AddressNormalizer.STREET_ABBREVIATIONS[lower])
            else:
                normalized.append(word)

        return ' '.join(normalized)
