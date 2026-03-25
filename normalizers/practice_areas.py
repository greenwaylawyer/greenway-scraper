"""Practice area normalization utilities.

Maps raw practice area tags from various sources to canonical taxonomy.
"""

from typing import Optional, Set
from utils.logger import get_logger

logger = get_logger(__name__)


class PracticeAreaNormalizer:
    """Normalize practice areas to canonical taxonomy."""

    # Canonical practice area taxonomy (~40 categories)
    CANONICAL_PRACTICE_AREAS = {
        'personal-injury',
        'criminal-defense',
        'family-law',
        'real-estate',
        'business-law',
        'immigration',
        'bankruptcy',
        'employment',
        'estate-planning',
        'medical-malpractice',
        'civil-litigation',
        'intellectual-property',
        'tax-law',
        'social-security',
        'workers-compensation',
        'traffic-violations',
        'environmental-law',
        'elder-law',
        'juvenile-law',
        'military-law',
        'civil-rights',
        'appellate-law',
        'construction-law',
        'consumer-protection',
        'corporate-law',
        'education-law',
        'entertainment-law',
        'health-care-law',
        'insurance-law',
        'international-law',
        'media-law',
        'municipal-law',
        'non-profit-law',
        'securities-law',
        'sports-law',
        'agriculture-law',
        'aviation-law',
        'banking-law',
        'energy-law',
        'maritime-law',
        'native-american-law',
    }

    # Mapping of common raw terms to canonical
    RAW_TO_CANONICAL = {
        # Personal Injury variations
        'personal injury': 'personal-injury',
        'injury law': 'personal-injury',
        'accident law': 'personal-injury',
        'car accident': 'personal-injury',
        'auto accident': 'personal-injury',
        'wrongful death': 'personal-injury',
        'slip and fall': 'personal-injury',

        # Criminal Defense variations
        'criminal law': 'criminal-defense',
        'criminal': 'criminal-defense',
        'dui': 'criminal-defense',
        'dwi': 'criminal-defense',
        'drug crimes': 'criminal-defense',
        'white collar': 'criminal-defense',
        'felony': 'criminal-defense',
        'misdemeanor': 'criminal-defense',

        # Family Law variations
        'divorce': 'family-law',
        'child custody': 'family-law',
        'child support': 'family-law',
        'spousal support': 'family-law',
        'alimony': 'family-law',
        'adoption': 'family-law',
        'domestic violence': 'family-law',
        'prenuptial': 'family-law',

        # Real Estate variations
        'real estate': 'real-estate',
        'property law': 'real-estate',
        'landlord tenant': 'real-estate',
        'foreclosure': 'real-estate',
        'zoning': 'real-estate',

        # Business Law variations
        'business': 'business-law',
        'corporate': 'corporate-law',
        'startup': 'business-law',
        'llc': 'business-law',
        'mergers and acquisitions': 'business-law',
        'contract law': 'business-law',

        # Immigration variations
        'immigration': 'immigration',
        'visa': 'immigration',
        'green card': 'immigration',
        'deportation': 'immigration',
        'citizenship': 'immigration',
        'asylum': 'immigration',

        # Employment variations
        'employment': 'employment',
        'labor law': 'employment',
        'workplace': 'employment',
        'discrimination': 'employment',
        'sexual harassment': 'employment',
        'wrongful termination': 'employment',

        # Estate Planning variations
        'estate planning': 'estate-planning',
        'wills': 'estate-planning',
        'trusts': 'estate-planning',
        'probate': 'estate-planning',
        'power of attorney': 'estate-planning',

        # IP variations
        'intellectual property': 'intellectual-property',
        'ip': 'intellectual-property',
        'patent': 'intellectual-property',
        'trademark': 'intellectual-property',
        'copyright': 'intellectual-property',

        # Tax variations
        'tax': 'tax-law',
        'irs': 'tax-law',
        'tax controversy': 'tax-law',
    }

    @staticmethod
    def normalize(raw_areas: Optional[list[str] | Set[str]]) -> Set[str]:
        """
        Normalize raw practice areas to canonical taxonomy.

        Args:
            raw_areas: List or set of raw practice area strings

        Returns:
            Set of canonical practice area identifiers
        """
        if not raw_areas:
            return set()

        normalized = set()

        for raw in raw_areas:
            if not raw:
                continue

            # Clean and normalize
            clean = raw.strip().lower()

            # Check direct mapping
            if clean in PracticeAreaNormalizer.RAW_TO_CANONICAL:
                normalized.add(PracticeAreaNormalizer.RAW_TO_CANONICAL[clean])
                continue

            # Check if already canonical
            if clean in PracticeAreaNormalizer.CANONICAL_PRACTICE_AREAS:
                normalized.add(clean)
                continue

            # Try partial matching
            for raw_key, canonical in PracticeAreaNormalizer.RAW_TO_CANONICAL.items():
                if raw_key in clean or clean in raw_key:
                    normalized.add(canonical)
                    break

        return normalized

    @staticmethod
    def score_completeness(practice_areas: Optional[list[str] | Set[str]]) -> int:
        """
        Score practice areas for completeness.

        Higher score for having multiple relevant practice areas.

        Args:
            practice_areas: List or set of practice areas

        Returns:
            Score from 0-12 (weighted field in overall scoring)
        """
        if not practice_areas:
            return 0

        normalized = PracticeAreaNormalizer.normalize(practice_areas)

        # Scoring:
        # 0 areas: 0 points
        # 1-2 areas: 4 points each
        # 3-5 areas: 3 points each (capped at 12)
        # 6+ areas: 2 points each (capped at 12)

        count = len(normalized)
        if count == 0:
            return 0
        elif count <= 2:
            return min(count * 4, 12)
        elif count <= 5:
            return min(count * 3, 12)
        else:
            return min(count * 2, 12)
