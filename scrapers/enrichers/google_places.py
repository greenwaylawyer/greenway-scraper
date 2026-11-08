"""
Google Places API enricher for Layer 4.

Collects ratings, reviews, geo-coordinates, and photos from Google Maps/Places.
"""

from typing import Dict, Any, List, Optional
import os
import re
import aiohttp
from urllib.parse import urlparse, parse_qs, unquote

from scrapers.enrichers.base_enricher import (
    BaseEnricher,
    DiscoveryCandidate,
    GENERIC_FIRM_WORDS,
    NAME_PREFIXES,
    NAME_SUFFIXES,
)
from utils.logger import get_logger

logger = get_logger(__name__)


class GooglePlacesEnricher(BaseEnricher):
    """Google Places API enrichment source for Layer 4."""

    FIND_PLACE_URL = "https://maps.googleapis.com/maps/api/place/findplacefromtext/json"
    PLACE_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
    
    # Fields to request from Find Place API
    # Note: findplacefromtext does NOT support formatted_phone_number (causes INVALID_REQUEST)
    FIND_PLACE_FIELDS = "place_id,name,formatted_address,geometry/location"
    
    # Fields to request from Place Details API (optimized for single call)
    DETAILS_FIELDS = "place_id,name,rating,user_ratings_total,reviews,photos,website,url,formatted_phone_number,geometry/location,formatted_address"
    
    def __init__(self):
        self.api_key = os.getenv('GOOGLE_PLACES_API_KEY')
        if not self.api_key:
            logger.warning("GOOGLE_PLACES_API_KEY not set in environment")
    
    def get_source_key(self) -> str:
        return 'google_maps'
    
    @staticmethod
    def extract_place_id(text: str) -> Optional[str]:
        """
        Extract place_id from various Google Maps URL formats or text input.
        
        Supports:
        - Direct place_id: ChIJ... (22+ chars)
        - Data parameter in URL: !1sChIJ...
        - Short URL: maps.app.goo.gl (requires HTTP follow - not supported here, return as-is)
        - Otherwise: treat as search query
        
        Args:
            text: URL or place_id string
        
        Returns:
            place_id string if found, otherwise None (caller should use search)
        """
        if not text:
            return None
        
        text = text.strip()
        
        # Direct place_id format
        if re.match(r'^ChIJ[A-Za-z0-9_-]{22,}$', text):
            return text
        
        # URL with !1s parameter (data=...!1sChIJ...)
        match = re.search(r'!1s(ChIJ[A-Za-z0-9_-]+)', text)
        if match:
            return match.group(1)
        
        # Short URL or other formats - return None, let caller use search
        if 'maps.app.goo.gl' in text or 'goo.gl/maps' in text:
            logger.info("Short URL detected, requires search fallback", url=text)
            return None
        
        # Not a recognizable place_id format
        return None

    @classmethod
    def _build_search_queries(cls, lawyer: Dict[str, Any]) -> List[str]:
        """
        Build Google Places Find Place search queries without wrapping names in exact-phrase
        double quotes (which causes multi-word/middle-name bar names to fail and fall back
        to generic city-wide attorney results).
        """
        identity = cls.extract_lawyer_identity(lawyer)
        loc_parts = [p for p in [lawyer.get('city'), identity['state_code'].upper() if identity['state_code'] else None] if p]
        loc_str = ' '.join(loc_parts)

        queries: List[str] = []

        if identity['full_name']:
            parts = [
                identity['full_name'],
                identity['firm_name'] or None,
                loc_str or None,
                'attorney',
            ]
            queries.append(' '.join(p for p in parts if p))

        # Fallback query with "First Last" when full_name has 3+ tokens (middle/maiden names)
        first_token = identity['first_token']
        last_token = identity['last_token']
        if first_token and last_token and first_token != last_token and len(identity['all_name_tokens']) >= 3:
            short_name = f"{first_token.capitalize()} {last_token.capitalize()}"
            parts = [short_name, loc_str or None, 'attorney']
            q = ' '.join(p for p in parts if p)
            if q not in queries:
                queries.append(q)

        # Fallback query with firm_name if available
        if identity['firm_name'] and identity['firm_tokens']:
            parts = [identity['firm_name'], loc_str or None, 'attorney']
            q = ' '.join(p for p in parts if p)
            if q not in queries:
                queries.append(q)

        return queries

    def _score_place_candidate(
        self,
        place: Dict[str, Any],
        lawyer: Dict[str, Any],
    ) -> Optional[DiscoveryCandidate]:
        """
        Score and validate a Google Places candidate.
        Returns None if the candidate fails the identity gate (e.g. only matches city/state).
        """
        place_id = place.get('place_id')
        name = (place.get('name') or '').strip()
        formatted_address = (place.get('formatted_address') or '').strip()

        if not place_id or not name:
            return None

        identity = self.extract_lawyer_identity(lawyer)

        # Extract street line1 ONLY (never city/state alone)
        raw_addr = lawyer.get('address', '')
        if isinstance(raw_addr, dict):
            lawyer_line1 = (raw_addr.get('line1') or '').strip()
        else:
            lawyer_line1 = (raw_addr or '').strip()

        confidence = 0.0
        signals: List[str] = []
        has_identity_match = False

        # 1. Street address (line1) match (+0.50)
        if self.is_valid_street_address(lawyer_line1) and formatted_address:
            addr_similarity = self.compare_addresses(lawyer_line1, formatted_address)
            if addr_similarity >= 0.5:
                confidence += 0.50
                signals.append(f'address_match_{int(round(addr_similarity * 100))}')
                has_identity_match = True

        # 2. Lawyer name / firm name match against place name
        cand_tokens = self.parse_person_name_tokens(name)
        cand_all_words = self.word_tokens(name)
        cand_sig_words = [
            w for w in cand_all_words
            if w not in GENERIC_FIRM_WORDS and w not in NAME_PREFIXES and w not in NAME_SUFFIXES
        ]

        first_token = identity['first_token']
        last_token = identity['last_token']

        has_first_and_last = bool(
            first_token
            and last_token
            and first_token in cand_tokens
            and last_token in cand_tokens
        )

        if has_first_and_last:
            confidence += 0.45
            signals.append('name_match')
            has_identity_match = True

        # Firm name match (+0.45, or +0.15 bonus if name_match also matched)
        firm_tokens = identity['firm_tokens']
        if firm_tokens and cand_sig_words:
            overlap = [t for t in firm_tokens if t in cand_sig_words]
            ratio = len(overlap) / len(firm_tokens)
            primary_firm_word = firm_tokens[0]
            if ratio >= 0.60 and primary_firm_word in cand_sig_words:
                confidence += 0.15 if has_first_and_last else 0.45
                signals.append('firm_name_match')
                has_identity_match = True

        # Last name match in a firm/practice listing (+0.30) when full first+last didn't match
        if not has_first_and_last and last_token and len(last_token) >= 3:
            if last_token in cand_sig_words:
                is_multi_partner = ('&' in name) or ('and' in cand_all_words)
                allowed_words = set(
                    identity['all_name_tokens']
                    + identity['surname_tokens']
                    + firm_tokens
                    + ([first_token[0]] if first_token else [])
                )
                foreign_words = [w for w in cand_sig_words if w not in allowed_words]
                if not foreign_words or is_multi_partner:
                    confidence += 0.30
                    signals.append('last_name_match')
                    has_identity_match = True

        # IDENTITY GATE: Reject candidates that only match city/state with zero identity match
        if not has_identity_match:
            return None

        # 3. Corroborating Location Signals (+0.10 city, +0.10 state)
        if identity['city'] and self.contains_phrase(formatted_address, identity['city']):
            confidence += 0.10
            signals.append('city_in_address')

        if self.location_matches_state(formatted_address, identity['state_code'], identity['state_name']):
            confidence += 0.10
            signals.append('state_in_address')

        confidence = min(1.0, round(confidence, 2))

        candidate = DiscoveryCandidate(
            url=place_id,  # For Google Places, "url" stores the place_id
            name=name,
            location=formatted_address,
            bar_number=None,
            confidence=confidence,
            match_signals=signals,
        )

        geometry = place.get('geometry') or {}
        location = geometry.get('location') or {}
        if location.get('lat') and location.get('lng'):
            candidate.match_signals.append(f"geo:{location['lat']},{location['lng']}")

        return candidate

    async def discover_profile(
        self,
        lawyer: Dict[str, Any],
    ) -> List[DiscoveryCandidate]:
        """
        Search Google Places for a matching business listing.
        
        Uses Find Place API with text query to locate the lawyer's office.
        Scores candidates based on address, phone, and name similarity.
        
        Cost: ~$0.017 per request
        
        Args:
            lawyer: Dict with full_name, firm_name, city, state, phone, address
        
        Returns:
            List of DiscoveryCandidate objects (usually 0 or 1)
        """
        if not self.api_key:
            logger.error("Cannot discover without GOOGLE_PLACES_API_KEY")
            return []

        queries = self._build_search_queries(lawyer)
        if not queries:
            return []

        candidates: List[DiscoveryCandidate] = []
        seen_place_ids = set()

        try:
            async with aiohttp.ClientSession() as session:
                for query in queries:
                    logger.info("Google Places discovery search", query=query, lawyer=lawyer.get('full_name'))
                    params = {
                        'input': query,
                        'inputtype': 'textquery',
                        'fields': self.FIND_PLACE_FIELDS,
                        'key': self.api_key,
                    }

                    async with session.get(self.FIND_PLACE_URL, params=params) as resp:
                        if resp.status != 200:
                            logger.error("Find Place API error", status=resp.status, text=await resp.text())
                            continue

                        data = await resp.json()

                        if data.get('status') not in ['OK', 'ZERO_RESULTS']:
                            logger.error(
                                "Find Place API bad status",
                                status=data.get('status'),
                                error=data.get('error_message'),
                            )
                            continue

                        if data.get('status') == 'ZERO_RESULTS':
                            logger.info("No Google Places results found", query=query)
                            continue

                        for place in data.get('candidates', []):
                            place_id = place.get('place_id')
                            if not place_id or place_id in seen_place_ids:
                                continue

                            candidate = self._score_place_candidate(place, lawyer)
                            if candidate is None:
                                continue

                            seen_place_ids.add(place_id)
                            candidates.append(candidate)

                            logger.info(
                                "Google Places candidate found",
                                place_id=place_id,
                                name=candidate.name,
                                confidence=candidate.confidence,
                                signals=candidate.match_signals,
                            )

                        # Stop trying fallback queries once we found at least one valid candidate
                        if candidates:
                            break

        except Exception as e:
            logger.error("Google Places discovery failed", error=str(e), lawyer=lawyer.get('full_name'))
            return []

        candidates.sort(key=lambda c: c.confidence, reverse=True)
        return candidates

    async def parse_profile(
        self,
        place_id: str,  # For Google Places, the "url" is actually the place_id
        lawyer: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """
        Fetch details for a Google Place.
        
        Uses Place Details API to retrieve ratings, reviews, photos, and coordinates.
        Returns all useful data in a single optimized call.
        
        Handles multiple place ID formats:
        - ChIJ... (standard format, use directly)
        - 0x...:0x... (hex ftid format, search to convert)
        - cid:12345 (CID format, search to convert)
        
        Cost: ~$0.017 per request (or $0.034 if conversion search needed)
        
        Args:
            place_id: Google place_id (stored from discovery)
            lawyer: Context about lawyer
        
        Returns:
            Dict with Google data or None if error
        """
        if not self.api_key:
            logger.error("Cannot fetch details without GOOGLE_PLACES_API_KEY")
            return None
        
        # Check if we need to convert the ID format
        if not place_id.startswith('ChIJ'):
            logger.info("Non-ChIJ format detected, converting", place_id=place_id)
            place_id = await self._convert_to_place_id(place_id, lawyer)
            if not place_id:
                logger.error("Failed to convert place ID to ChIJ format")
                return None
        
        logger.info("Fetching Google Place details", place_id=place_id, lawyer=lawyer.get('full_name'))
        
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    'place_id': place_id,
                    'fields': self.DETAILS_FIELDS,
                    'key': self.api_key,
                }
                
                async with session.get(self.PLACE_DETAILS_URL, params=params) as resp:
                    if resp.status != 200:
                        logger.error("Place Details API error", status=resp.status, text=await resp.text())
                        return None
                    
                    data = await resp.json()
                    
                    if data.get('status') not in ['OK']:
                        logger.error("Place Details API bad status", status=data.get('status'),
                                   error=data.get('error_message'))
                        return None
                    
                    result = data.get('result', {})
                    
                    # Extract geo coordinates
                    geometry = result.get('geometry', {})
                    location = geometry.get('location', {})
                    geo_lat = location.get('lat')
                    geo_lng = location.get('lng')
                    
                    # Extract reviews (max 5 from API)
                    reviews_raw = result.get('reviews', [])
                    reviews = []
                    for review in reviews_raw[:5]:  # Ensure max 5
                        reviews.append({
                            'author': review.get('author_name', 'Anonymous'),
                            'rating': review.get('rating'),
                            'text': review.get('text', ''),
                            'time': review.get('time'),  # Unix timestamp
                            'relative_time': review.get('relative_time_description', ''),
                        })
                    
                    # Extract photo references (convert to URLs)
                    photos_raw = result.get('photos', [])
                    photos = []
                    for photo in photos_raw[:5]:  # Limit to 5
                        photo_ref = photo.get('photo_reference')
                        if photo_ref:
                            # Build photo URL
                            photo_url = (
                                f"https://maps.googleapis.com/maps/api/place/photo"
                                f"?maxwidth=800&photoreference={photo_ref}&key={self.api_key}"
                            )
                            photos.append(photo_url)
                    
                    # Build structured data
                    profile_data = {
                        'place_id': place_id,
                        'name': result.get('name'),
                        'google_rating': result.get('rating'),
                        'google_review_count': result.get('user_ratings_total'),
                        'google_reviews': reviews,
                        'google_photos': photos,
                        'google_maps_url': result.get('url', f"https://maps.google.com/?cid={place_id}"),
                        'formatted_phone': result.get('formatted_phone_number'),
                        'formatted_address': result.get('formatted_address'),
                        'website': result.get('website'),
                        'geo_lat': geo_lat,
                        'geo_lng': geo_lng,
                        'scraped_at': None,  # Will be set by worker
                    }
                    
                    logger.info("Google Place details retrieved", 
                              place_id=place_id,
                              rating=profile_data['google_rating'],
                              review_count=profile_data['google_review_count'],
                              has_photos=len(photos) > 0)
                    
                    return profile_data
        
        except Exception as e:
            logger.error("Google Places parse_profile failed", error=str(e), place_id=place_id)
            return None
    
    async def _convert_to_place_id(
        self,
        raw_id: str,
        lawyer: Dict[str, Any],
    ) -> Optional[str]:
        """
        Convert non-ChIJ format IDs to standard place_id.
        
        Handles:
        - Hex format (0x...:0x...)
        - CID format (cid:12345)
        
        Uses findplacefromtext with the lawyer's name/location to search.
        
        Args:
            raw_id: Raw ID in hex or CID format
            lawyer: Lawyer context for search query
        
        Returns:
            ChIJ format place_id or None
        """
        candidates = await self.discover_profile(lawyer)
        if not candidates:
            logger.warning("No validated candidates found during ID conversion", raw_id=raw_id)
            return None

        converted_id = candidates[0].url
        logger.info(
            "Successfully converted place ID",
            original=raw_id,
            converted=converted_id,
            name=candidates[0].name,
        )
        return converted_id
