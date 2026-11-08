"""Base enricher class for Layer 3 and Layer 4 source scrapers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, Any, List, Optional
from difflib import SequenceMatcher
import re


NAME_PREFIXES = {
    'mr', 'mrs', 'ms', 'miss', 'dr', 'prof', 'hon', 'judge', 'attorney', 'lawyer',
}

NAME_SUFFIXES = {
    'jr', 'sr', 'ii', 'iii', 'iv', 'v', 'vi', 'esq', 'esquire', 'md', 'phd', 'jd', 'llm',
}

GENERIC_FIRM_WORDS = {
    'law', 'firm', 'firms', 'office', 'offices', 'group', 'llc', 'pc', 'pllc', 'pa',
    'inc', 'llp', 'ltd', 'co', 'corp', 'corporation', 'company', 'associates',
    'associate', 'partners', 'partner', 'attorney', 'attorneys', 'lawyer', 'lawyers',
    'legal', 'services', 'service', 'center', 'centre', 'practice', 'counsel',
    'counselor', 'counselors', 'professional', 'the', 'of', 'and', 'at', 'in', 'for',
    'a', 'an', 'by', 'pl', 'plc', 'apc', 'lpa', 'psc', 'chtd', 'chartered',
    'immigration', 'criminal', 'family', 'personal', 'injury', 'defense', 'litigation',
}

STREET_ABBREVIATIONS = {
    'street': 'st',
    'avenue': 'ave',
    'boulevard': 'blvd',
    'parkway': 'pkwy',
    'drive': 'dr',
    'road': 'rd',
    'court': 'ct',
    'lane': 'ln',
    'place': 'pl',
    'circle': 'cir',
    'highway': 'hwy',
    'expressway': 'expy',
    'freeway': 'fwy',
    'terrace': 'ter',
    'trail': 'trl',
    'way': 'way',
    'square': 'sq',
    'suite': 'ste',
    'building': 'bldg',
    'floor': 'fl',
    'apartment': 'apt',
    'north': 'n',
    'south': 's',
    'east': 'e',
    'west': 'w',
    'northeast': 'ne',
    'northwest': 'nw',
    'southeast': 'se',
    'southwest': 'sw',
}

STATE_NAMES = {
    'al': 'alabama', 'ak': 'alaska', 'az': 'arizona', 'ar': 'arkansas',
    'ca': 'california', 'co': 'colorado', 'ct': 'connecticut', 'de': 'delaware',
    'fl': 'florida', 'ga': 'georgia', 'hi': 'hawaii', 'id': 'idaho',
    'il': 'illinois', 'in': 'indiana', 'ia': 'iowa', 'ks': 'kansas',
    'ky': 'kentucky', 'la': 'louisiana', 'me': 'maine', 'md': 'maryland',
    'ma': 'massachusetts', 'mi': 'michigan', 'mn': 'minnesota', 'ms': 'mississippi',
    'mo': 'missouri', 'mt': 'montana', 'ne': 'nebraska', 'nv': 'nevada',
    'nh': 'new hampshire', 'nj': 'new jersey', 'nm': 'new mexico', 'ny': 'new york',
    'nc': 'north carolina', 'nd': 'north dakota', 'oh': 'ohio', 'ok': 'oklahoma',
    'or': 'oregon', 'pa': 'pennsylvania', 'ri': 'rhode island', 'sc': 'south carolina',
    'sd': 'south dakota', 'tn': 'tennessee', 'tx': 'texas', 'ut': 'utah',
    'vt': 'vermont', 'va': 'virginia', 'wa': 'washington', 'wv': 'west virginia',
    'wi': 'wisconsin', 'wy': 'wyoming', 'dc': 'district of columbia',
}

STATE_NAME_TO_CODE = {v: k for k, v in STATE_NAMES.items()}


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
    def word_tokens(text: Optional[str]) -> List[str]:
        """Extract lowercase alphanumeric word tokens."""
        if not text:
            return []
        return re.findall(r'[a-z0-9]+', text.lower())

    @staticmethod
    def parse_person_name_tokens(name: Optional[str]) -> List[str]:
        """
        Normalize a person's name into clean lowercase tokens, handling 'Last, First'
        and stripping honorific prefixes and professional/generational suffixes.
        """
        if not name or not name.strip():
            return []

        raw = name.strip()
        if raw.count(',') == 1:
            before, after = [p.strip() for p in raw.split(',', 1)]
            after_words = BaseEnricher.word_tokens(after)
            if after_words and not (set(after_words) & (NAME_SUFFIXES | GENERIC_FIRM_WORDS)):
                raw = f"{after} {before}"

        tokens = BaseEnricher.word_tokens(raw)
        while tokens and tokens[0] in NAME_PREFIXES:
            tokens.pop(0)
        while tokens and tokens[-1] in NAME_SUFFIXES:
            tokens.pop()

        return tokens

    @staticmethod
    def distinctive_firm_tokens(firm_name: Optional[str]) -> List[str]:
        """Extract non-generic distinctive tokens from a law firm name."""
        if not firm_name or not firm_name.strip():
            return []
        tokens = BaseEnricher.word_tokens(firm_name)
        seen = []
        for t in tokens:
            if (
                len(t) >= 3
                and t not in GENERIC_FIRM_WORDS
                and t not in NAME_PREFIXES
                and t not in NAME_SUFFIXES
                and t not in seen
            ):
                seen.append(t)
        return seen

    @staticmethod
    def extract_lawyer_identity(lawyer: Dict[str, Any]) -> Dict[str, Any]:
        """Extract normalized identity tokens and location attributes from lawyer dict."""
        full_name = (lawyer.get('full_name') or '').strip()
        parsed_tokens = BaseEnricher.parse_person_name_tokens(full_name)

        first_raw = (lawyer.get('first_name') or '').strip()
        last_raw = (lawyer.get('last_name') or '').strip()

        first_tokens = BaseEnricher.word_tokens(first_raw) if first_raw else []
        last_tokens = BaseEnricher.word_tokens(last_raw) if last_raw else []

        first_token = first_tokens[0] if first_tokens else (parsed_tokens[0] if parsed_tokens else '')
        last_token = last_tokens[-1] if last_tokens else (parsed_tokens[-1] if parsed_tokens else '')

        all_name_tokens = []
        for w in parsed_tokens + first_tokens + last_tokens:
            if len(w) >= 2 and w not in all_name_tokens:
                all_name_tokens.append(w)

        surname_tokens = []
        for w in (last_tokens + ([last_token] if last_token else []) + ([parsed_tokens[-2]] if len(parsed_tokens) >= 4 else [])):
            if len(w) >= 3 and w not in surname_tokens:
                surname_tokens.append(w)

        firm_name = (lawyer.get('firm_name') or '').strip()
        firm_tokens = BaseEnricher.distinctive_firm_tokens(firm_name)

        city = (lawyer.get('city') or '').strip().lower()
        state_raw = (lawyer.get('state') or lawyer.get('license_state') or '').strip().lower()
        state_code = state_raw if len(state_raw) == 2 else STATE_NAME_TO_CODE.get(state_raw, state_raw)
        state_name = STATE_NAMES.get(state_code, '')

        return {
            'full_name': full_name,
            'first_token': first_token,
            'last_token': last_token,
            'all_name_tokens': all_name_tokens,
            'surname_tokens': surname_tokens,
            'firm_name': firm_name,
            'firm_tokens': firm_tokens,
            'city': city,
            'state_code': state_code,
            'state_name': state_name,
        }

    @staticmethod
    def contains_phrase(text: Optional[str], phrase: Optional[str]) -> bool:
        """Check if text contains phrase on word boundaries."""
        if not text or not phrase:
            return False
        p = phrase.strip().lower()
        t = text.strip().lower()
        if not p or not t:
            return False
        return bool(re.search(r'\b' + re.escape(p) + r'\b', t))

    @staticmethod
    def location_matches_state(location: Optional[str], state_code: str, state_name: str = '') -> bool:
        """Check if location matches state code or full state name on word boundaries."""
        if not location:
            return False
        if state_code and BaseEnricher.contains_phrase(location, state_code):
            return True
        if state_name and BaseEnricher.contains_phrase(location, state_name):
            return True
        return False

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
        
        Identity Gate:
        A candidate on an individual lawyer directory (Justia/Avvo) must match
        either the bar number, OR (last name + first name / initial / middle name token),
        OR have >= 0.75 name similarity. Candidates that only match city/state are
        assigned 0.0 confidence so unrelated lawyers in the same city are never surfaced.
        
        Args:
            candidate: The candidate to score
            lawyer: The target lawyer data
        
        Returns:
            Confidence score 0.0-1.0
        """
        score = 0.0
        signals = []

        # Bar number exact match → instant high confidence
        cand_bar = (candidate.bar_number or '').strip().lower()
        target_bar = str(lawyer.get('bar_number') or '').strip().lower()
        if cand_bar and target_bar and cand_bar not in ('n/a', 'none') and cand_bar == target_bar:
            score = 1.00
            signals.append('bar_number_exact')
            candidate.match_signals = signals
            candidate.confidence = score
            return score

        identity = BaseEnricher.extract_lawyer_identity(lawyer)
        cand_tokens = BaseEnricher.parse_person_name_tokens(candidate.name)

        if not cand_tokens:
            candidate.match_signals = []
            candidate.confidence = 0.0
            return 0.0

        target_first = identity['first_token']
        target_last = identity['last_token']

        last_matched = bool(
            target_last and (
                target_last in cand_tokens
                or any(s in cand_tokens for s in identity['surname_tokens'])
            )
        )
        first_matched = bool(
            target_first and (
                target_first in cand_tokens
                or cand_tokens[0] in identity['all_name_tokens']
            )
        )
        first_initial_matched = bool(
            target_first
            and len(cand_tokens[0]) == 1
            and cand_tokens[0] == target_first[0]
        )

        if last_matched:
            score += 0.30
            signals.append('last_name_match')

        if first_matched:
            score += 0.20
            signals.append('first_name_match')
        elif first_initial_matched:
            score += 0.10
            signals.append('first_initial_match')

        # Name similarity (compare both full normalized name and first+last name so middle names don't penalize)
        similarity = 0.0
        if identity['all_name_tokens'] and cand_tokens:
            target_full_norm = ' '.join(identity['all_name_tokens'])
            cand_full_norm = ' '.join(cand_tokens)
            similarity = SequenceMatcher(None, target_full_norm, cand_full_norm).ratio()

            if target_first and target_last:
                target_short = f"{target_first} {target_last}"
                cand_short = f"{cand_tokens[0]} {cand_tokens[-1]}"
                similarity = max(similarity, SequenceMatcher(None, target_short, cand_short).ratio())

        # Identity Gate: do not award location points to unrelated people
        passes_identity_gate = (last_matched and (first_matched or first_initial_matched)) or similarity >= 0.75
        if not passes_identity_gate:
            candidate.match_signals = []
            candidate.confidence = 0.0
            return 0.0

        # City
        candidate_location = (candidate.location or '').lower()
        target_city = identity['city']
        if target_city and BaseEnricher.contains_phrase(candidate_location, target_city):
            score += 0.20
            signals.append('city_match')

        # State (word-boundary check for 2-letter code or full state name)
        if BaseEnricher.location_matches_state(candidate_location, identity['state_code'], identity['state_name']):
            score += 0.10
            signals.append('state_match')

        if similarity >= 0.8:
            score += min(0.20, similarity * 0.25)
            signals.append(f'name_similarity_{int(round(similarity * 100))}')
        
        # Cap at 1.0
        score = min(1.0, round(score, 2))
        
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
    def _normalize_address_tokens(addr: str) -> List[str]:
        """Normalize address tokens using standard USPS street suffix abbreviations."""
        words = BaseEnricher.word_tokens(addr)
        skip = {'usa', 'united', 'states', 'ste', 'suite', 'apt', 'unit', 'bldg', 'building', 'fl', 'floor'}
        return [STREET_ABBREVIATIONS.get(w, w) for w in words if w not in skip]

    @staticmethod
    def is_valid_street_address(addr: Optional[str]) -> bool:
        """True only if addr contains a street number and at least one street name word (not just 'City, ST')."""
        if not addr or not addr.strip():
            return False
        tokens = BaseEnricher.word_tokens(addr)
        if len(tokens) < 2:
            return False
        has_digits = any(any(c.isdigit() for c in t) for t in tokens)
        has_alpha = any(not t.isdigit() for t in tokens)
        return has_digits and has_alpha

    @staticmethod
    def compare_addresses(addr1: str, addr2: str) -> float:
        """
        Compare two addresses for street-level similarity.
        
        Args:
            addr1: First address string (lawyer street address)
            addr2: Second address string (candidate formatted address)
        
        Returns:
            Similarity score 0.0-1.0
        """
        if not BaseEnricher.is_valid_street_address(addr1) or not addr2:
            return 0.0

        street_part2 = addr2.split(',')[0] if ',' in addr2 else addr2
        tokens1 = BaseEnricher._normalize_address_tokens(addr1)
        tokens2_street = BaseEnricher._normalize_address_tokens(street_part2)
        tokens2_full = BaseEnricher._normalize_address_tokens(addr2)

        if not tokens1 or not tokens2_street:
            return 0.0

        # If addr1 has a street number, addr2 must contain that exact street number
        numbers1 = [t for t in tokens1 if t.isdigit()]
        if numbers1 and numbers1[0] not in tokens2_full:
            return 0.0

        set1 = set(tokens1)
        set2_street = set(tokens2_street)
        set2_full = set(tokens2_full)

        overlap_street = len(set1 & set2_street)
        union_street = len(set1 | set2_street)
        jaccard = overlap_street / union_street if union_street > 0 else 0.0

        coverage = len(set1 & set2_full) / len(set1) if set1 else 0.0
        return max(jaccard, coverage * 0.85)
