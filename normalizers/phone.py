"""Phone number normalization utilities.

Converts phone numbers to E.164 format: +1XXX XXXXXXX
"""

import re
from typing import Optional
from utils.logger import get_logger

logger = get_logger(__name__)


class PhoneNormalizer:
    """Normalize phone numbers to E.164 format."""

    # Regex pattern to extract digits from phone strings
    PHONE_PATTERN = re.compile(r'''
        (?:                    # Non-capturing group for optional prefixes
            \+?1?              # Optional country code +1 or just 1
            [\s\-.]?           # Optional separator
        )?
        (\(?\d{3}\)?)          # Area code with optional parentheses
        [\s\-.]?               # Separator
        \d{3}                  # Exchange code
        [\s\-.]?               # Separator
        \d{4}                  # Line number
    ''', re.VERBOSE)

    @staticmethod
    def normalize(phone: Optional[str], default_country: str = 'US') -> Optional[str]:
        """
        Normalize a phone number to E.164 format.

        Args:
            phone: Raw phone number string
            default_country: Default country code (default: US = +1)

        Returns:
            E.164 formatted phone number (+1XXX XXXXXXX) or None if invalid
        """
        if not phone or not phone.strip():
            return None

        # Remove all non-digit characters except +
        cleaned = re.sub(r'[^\d+]', '', phone)

        # Check if it's a valid 10-digit number
        if len(cleaned) == 10 and cleaned.isdigit():
            return f"+1{cleaned}"

        # Check if it already has country code
        if cleaned.startswith('+1') and len(cleaned) == 12:
            return cleaned

        # Check if it has country code without +
        if cleaned.startswith('1') and len(cleaned) == 11:
            return f"+{cleaned}"

        # Try to extract using regex
        match = PhoneNormalizer.PHONE_PATTERN.search(phone)
        if match:
            digits = re.sub(r'\D', '', match.group(0))
            if len(digits) == 10:
                return f"+1{digits}"

        # International number — keep the raw value (strip excessive whitespace)
        # and log at debug level only (non-US formats are expected for NY attorneys abroad)
        raw = " ".join(phone.split())
        if raw:
            logger.debug("Storing non-US phone number as-is", phone=raw)
            return raw

        return None
