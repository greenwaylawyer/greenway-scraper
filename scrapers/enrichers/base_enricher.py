"""Base enricher class for Layer 3 and Layer 4 source scrapers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from difflib import SequenceMatcher


@dataclass
class DiscoveryCandidate:
    """
    Represents a candidate profile found during discovery.
    
    Attributes:
        url: Direct URL to the candidate profile
        name: Full name found on the source
        location: City and state (e.g., "Los Angeles, CA")
        bar_number: Bar number if displayed on source, else None
        confidence: Calculated confidence score 0.0-1.0
        match_signals: List of matching criteria (e.g., ['bar_number_exact', 'city_match'])
    """
    url: str
    name: str
    location: str
    bar_number: Optional[str] = None
    confidence: float = 0.0
    match_signals: List[str] = field(default_factory=list)

    def to_dict(self) -> Dict[str, Any]:
        """Convert to dict for JSON storage."""
        return {
            'url': self.url,
            'name': self.name,
            'location': self.location,
            'bar_number': self.bar_number,
            'confidence': self.confidence,
            'match_signals': self.match_signals,
        }


class BaseEnricher(ABC):
    """
    Abstract base class for all enrichment sources (Layer 3 and Layer 4).
    
    Each source scraper (Justia, Avvo, Google Maps, etc.) must extend this
    and implement the abstract methods.
    """

    @abstractmethod
    async def discover_profile(
        self,
        lawyer: Dict[str, Any],
    ) -> List[DiscoveryCandidate]:
        """
        Search the source for a matching profile.
        
        Args:
            lawyer: Dict with lawyer data including:
                - full_name, first_name, last_name
                - bar_number
                - city, state (license_state)
                - phone (if available)
                - firm_name (if available)
        
        Returns:
            List of DiscoveryCandidate objects with confidence scores.
            Empty list means no match found.
            
        The implementation should:
        1. Run each discovery strategy defined in source config
        2. Score each candidate using score_candidate()
        3. Return all candidates, sorted by confidence descending
        """
        pass

    @abstractmethod
    async def parse_profile(
        self,
        url: str,
        lawyer: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Scrape and return all available data from the profile URL.
        
        Args:
            url: Direct URL to the profile (or place_id for Google Maps)
            lawyer: Context about the lawyer being enriched
        
        Returns:
            Dict with extracted data. Keys should match fields_provided in source config.
            Return None if profile page is 404 or contains no useful data.
            
        The returned dict is stored verbatim in scraped_data JSONB.
        Keys vary by source — no schema validation beyond "must be dict".
        
        Example for Justia:
        {
            "bio": "Attorney since 2005...",
            "education_history": [{"school": "UCLA Law", "degree": "JD", "year": 2005}],
            "languages": ["English", "Spanish"],
            "websites": ["https://johndoelaw.com"],
            "social_links": {"linkedin": "...", "twitter": "..."},
            "photo_url": "https://justia.com/...",
            "practice_areas": ["Criminal Defense", "DUI"]
        }
        """
        pass

    def get_source_key(self) -> str:
        """
        Return the source identifier (matches YAML key).
        
        Default implementation: lowercase class name without 'Enricher' suffix.
        Override if needed.
        """
        class_name = self.__class__.__name__
        # Remove 'Enricher' suffix if present
        if class_name.endswith('Enricher'):
            class_name = class_name[:-8]
        return class_name.lower()

    @staticmethod
    def score_candidate(
        candidate: DiscoveryCandidate,
        lawyer: Dict[str, Any],
    ) -> float:
        """
        Calculate confidence score for a discovery candidate.
        
        Scoring rules:
        - Bar number exact match:       +0.20 (instant 1.00 if only match)
        - Last name exact match:        +0.30
        - First name exact match:       +0.20
        - City exact match:             +0.20
        - State exact match:            +0.10
        - Name similarity (fuzzy):      +0.00 to +0.20
        
        Args:
            candidate: The candidate to score
            lawyer: The target lawyer data
        
        Returns:
            Confidence score 0.0-1.0
        """
        score = 0.0
        signals = []

        # Bar number exact match → instant high confidence
        if candidate.bar_number and lawyer.get('bar_number'):
            if candidate.bar_number.strip().lower() == str(lawyer['bar_number']).strip().lower():
                score = 1.00
                signals.append('bar_number_exact')
                candidate.match_signals = signals
                candidate.confidence = score
                return score

        # Last name — use (or '') to guard against None values in DB
        candidate_name_parts = (candidate.name or '').lower().split()
        target_last = (lawyer.get('last_name') or '').lower()
        target_first = (lawyer.get('first_name') or '').lower()

        if target_last and target_last in candidate_name_parts:
            score += 0.30
            signals.append('last_name_match')

        # First name
        if target_first and target_first in candidate_name_parts:
            score += 0.20
            signals.append('first_name_match')

        # City
        candidate_location = (candidate.location or '').lower()
        target_city = (lawyer.get('city') or '').lower()
        if target_city and target_city in candidate_location:
            score += 0.20
            signals.append('city_match')

        # State
        target_state = (lawyer.get('state') or '').lower()
        if not target_state:
            target_state = (lawyer.get('license_state') or '').lower()
        if target_state and target_state in candidate_location:
            score += 0.10
            signals.append('state_match')

        # Name similarity (fuzzy match as bonus)
        target_full = (lawyer.get('full_name') or '').lower()
        candidate_full = (candidate.name or '').lower()
        if target_full and candidate_full:
            similarity = SequenceMatcher(None, target_full, candidate_full).ratio()
            if similarity >= 0.8:
                score += min(0.20, similarity * 0.25)
                signals.append(f'name_similarity_{int(similarity * 100)}')
        
        # Cap at 1.0
        score = min(1.0, score)
        
        candidate.match_signals = signals
        candidate.confidence = score
        return score

    @staticmethod
    def normalize_phone(phone: str) -> Optional[str]:
        """
        Normalize phone number to E.164 format for comparison.
        
        Args:
            phone: Raw phone number string
        
        Returns:
            Normalized phone like "+12135551234" or None if invalid
        """
        if not phone:
            return None
        
        # Strip all non-digits
        digits = ''.join(c for c in phone if c.isdigit())
        
        # Must be 10 or 11 digits
        if len(digits) == 10:
            return f"+1{digits}"
        elif len(digits) == 11 and digits[0] == '1':
            return f"+{digits}"
        else:
            return None

    @staticmethod
    def compare_addresses(addr1: str, addr2: str) -> float:
        """
        Compare two addresses for similarity.
        
        Args:
            addr1: First address string
            addr2: Second address string
        
        Returns:
            Similarity score 0.0-1.0
        """
        if not addr1 or not addr2:
            return 0.0
        
        # Normalize: lowercase, remove punctuation
        a1 = ''.join(c.lower() if c.isalnum() or c.isspace() else ' ' for c in addr1)
        a2 = ''.join(c.lower() if c.isalnum() or c.isspace() else ' ' for c in addr2)
        
        # Simple word overlap
        words1 = set(a1.split())
        words2 = set(a2.split())
        
        if not words1 or not words2:
            return 0.0
        
        overlap = len(words1 & words2)
        total = len(words1 | words2)
        
        return overlap / total if total > 0 else 0.0
