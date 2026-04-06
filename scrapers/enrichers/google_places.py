"""
Google Places API enricher for Layer 4.

Collects ratings, reviews, geo-coordinates, and photos from Google Maps/Places.
"""

from typing import Dict, Any, List, Optional
import os
import re
import aiohttp
from urllib.parse import urlparse, parse_qs, unquote

from scrapers.enrichers.base_enricher import BaseEnricher, DiscoveryCandidate
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
        
        candidates = []
        
        # Build search query: "Full Name" "Firm Name" City State attorney
        query_parts = []
        if lawyer.get('full_name'):
            query_parts.append(f'"{lawyer["full_name"]}"')
        if lawyer.get('firm_name'):
            query_parts.append(f'"{lawyer["firm_name"]}"')
        if lawyer.get('city'):
            query_parts.append(lawyer['city'])
        if lawyer.get('state') or lawyer.get('license_state'):
            query_parts.append(lawyer.get('state') or lawyer.get('license_state'))
        query_parts.append('attorney')
        
        query = ' '.join(query_parts)
        
        logger.info("Google Places discovery search", query=query, lawyer=lawyer.get('full_name'))
        
        # Make API call
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    'input': query,
                    'inputtype': 'textquery',
                    'fields': self.FIND_PLACE_FIELDS,
                    'key': self.api_key,
                }
                
                async with session.get(self.FIND_PLACE_URL, params=params) as resp:
                    if resp.status != 200:
                        logger.error("Find Place API error", status=resp.status, text=await resp.text())
                        return []
                    
                    data = await resp.json()
                    
                    if data.get('status') not in ['OK', 'ZERO_RESULTS']:
                        logger.error("Find Place API bad status", status=data.get('status'), 
                                   error=data.get('error_message'))
                        return []
                    
                    if data.get('status') == 'ZERO_RESULTS':
                        logger.info("No Google Places results found", query=query)
                        return []
                    
                    # Parse candidates (usually just 1)
                    for place in data.get('candidates', []):
                        place_id = place.get('place_id')
                        if not place_id:
                            continue
                        
                        name = place.get('name', '')
                        formatted_address = place.get('formatted_address', '')
                        geometry = place.get('geometry', {})
                        location = geometry.get('location', {})
                        
                        # Extract city/state from formatted_address
                        # Example: "123 Main St, Los Angeles, CA 90001, USA"
                        location_str = formatted_address
                        
                        # Score candidate
                        confidence = 0.0
                        signals = []
                        
                        # Address match (+0.50)
                        lawyer_address = lawyer.get('address', '')
                        if isinstance(lawyer_address, dict):
                            # merged_data format
                            addr_parts = [
                                lawyer_address.get('line1', ''),
                                lawyer_address.get('city', ''),
                                lawyer_address.get('state', ''),
                            ]
                            lawyer_address = ', '.join(p for p in addr_parts if p)
                        
                        if lawyer_address and formatted_address:
                            addr_similarity = self.compare_addresses(lawyer_address, formatted_address)
                            if addr_similarity >= 0.5:
                                confidence += 0.50
                                signals.append(f'address_match_{int(addr_similarity * 100)}')
                        # Phone matching is deferred to place/details (findplacefromtext doesn't return phone)
                        
                        # Name similarity (+0.15)
                        lawyer_name = lawyer.get('full_name', '')
                        if lawyer_name and name:
                            from difflib import SequenceMatcher
                            lower_lawyer = lawyer_name.lower().strip()
                            lower_name = name.lower()
                            if lower_lawyer and lower_lawyer in lower_name:
                                confidence += 0.15
                                signals.append('name_contains')
                            else:
                                name_sim = SequenceMatcher(None, lower_lawyer, lower_name).ratio()
                                if name_sim >= 0.6:
                                    confidence += 0.15 * (name_sim / 0.6)  # Scale: 0.6->0.15, 1.0->0.25
                                    signals.append(f'name_similarity_{int(name_sim * 100)}')

                        # City/state presence in formatted_address (+0.20)
                        target_city = (lawyer.get('city') or '').lower()
                        target_state = (lawyer.get('state') or lawyer.get('license_state') or '').lower()
                        addr_lower = (formatted_address or '').lower()
                        if target_city and target_city in addr_lower:
                            confidence += 0.10
                            signals.append('city_in_address')
                        if target_state and target_state in addr_lower:
                            confidence += 0.10
                            signals.append('state_in_address')

                        confidence = min(1.0, confidence)
                        
                        candidate = DiscoveryCandidate(
                            url=place_id,  # For Google Places, "url" stores the place_id
                            name=name,
                            location=location_str,
                            bar_number=None,  # Google doesn't show bar numbers
                            confidence=confidence,
                            match_signals=signals,
                        )
                        
                        # Store lat/lng in match_signals for later use
                        if location.get('lat') and location.get('lng'):
                            candidate.match_signals.append(f"geo:{location['lat']},{location['lng']}")
                        
                        candidates.append(candidate)
                        
                        logger.info("Google Places candidate found", 
                                  place_id=place_id, 
                                  name=name,
                                  confidence=confidence,
                                  signals=signals)
        
        except Exception as e:
            logger.error("Google Places discovery failed", error=str(e), query=query)
            return []
        
        # Sort by confidence descending
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
        # Build search query from lawyer data
        query_parts = []
        if lawyer.get('full_name'):
            query_parts.append(f'"{lawyer["full_name"]}"')
        if lawyer.get('firm_name'):
            query_parts.append(f'"{lawyer["firm_name"]}"')
        if lawyer.get('city'):
            query_parts.append(lawyer['city'])
        if lawyer.get('state') or lawyer.get('license_state'):
            query_parts.append(lawyer.get('state') or lawyer.get('license_state'))
        query_parts.append('attorney')
        
        query = ' '.join(query_parts)
        
        logger.info("Converting place ID via search", raw_id=raw_id, query=query)
        
        try:
            async with aiohttp.ClientSession() as session:
                params = {
                    'input': query,
                    'inputtype': 'textquery',
                    'fields': 'place_id,name',
                    'key': self.api_key,
                }
                
                async with session.get(self.FIND_PLACE_URL, params=params) as resp:
                    if resp.status != 200:
                        logger.error("Find Place API error during conversion", status=resp.status)
                        return None
                    
                    data = await resp.json()
                    
                    if data.get('status') != 'OK':
                        logger.error("Find Place API bad status during conversion", status=data.get('status'))
                        return None
                    
                    candidates = data.get('candidates', [])
                    if not candidates:
                        logger.warning("No candidates found during ID conversion")
                        return None
                    
                    # Return the first candidate's place_id
                    converted_id = candidates[0].get('place_id')
                    logger.info("Successfully converted place ID", 
                              original=raw_id, 
                              converted=converted_id,
                              name=candidates[0].get('name'))
                    return converted_id
        
        except Exception as e:
            logger.error("Failed to convert place ID", error=str(e), raw_id=raw_id)
            return None
