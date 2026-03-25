"""Completeness scoring utilities.

Scores lawyer records from 0-100 based on field completeness.
Higher scores indicate more complete profiles.
"""

from typing import Optional
from scrapers.base import LawyerRawData
from normalizers.practice_areas import PracticeAreaNormalizer
from utils.logger import get_logger

logger = get_logger(__name__)


class CompletenessScorer:
    """Score lawyer records for completeness."""

    # Field weights (sum = 100)
    WEIGHTS = {
        'practice_areas': 12,
        'full_name': 10,
        'bio': 10,
        'license_status': 10,
        'photo_url': 8,
        'bar_number': 8,
        'city': 8,
        'state': 8,
        'phone': 6,
        'law_school': 5,
        'admission_date': 5,
        'firm_name': 5,
        'website_url': 3,
        'email': 2,
    }

    @staticmethod
    def score(data: LawyerRawData | dict) -> int:
        """
        Calculate completeness score for a lawyer record.

        Args:
            data: LawyerRawData object or dict with lawyer data

        Returns:
            Score from 0-100
        """
        score = 0

        # Extract fields (handle both LawyerRawData and dict)
        if isinstance(data, LawyerRawData):
            practice_areas = data.practice_areas
            full_name = data.full_name
            bio = data.bio
            license_status = data.license_status
            photo_url = data.photo_url
            bar_number = data.bar_number
            address = data.address
            phone = data.phone
            law_school = data.law_school
            admission_year = data.admission_year
            firm_name = data.firm_name
            website_url = data.website_url
            email = data.email
        else:
            practice_areas = data.get('practice_areas')
            full_name = data.get('full_name')
            bio = data.get('bio')
            license_status = data.get('license_status')
            photo_url = data.get('photo_url')
            bar_number = data.get('bar_number')
            address = data.get('address') or data.get('address_line1')
            phone = data.get('phone')
            law_school = data.get('law_school')
            admission_year = data.get('admission_year') or data.get('admission_date')
            firm_name = data.get('firm_name')
            website_url = data.get('website_url')
            email = data.get('email')

        # Score practice areas
        if practice_areas:
            score += PracticeAreaNormalizer.score_completeness(practice_areas)

        # Score full name
        if full_name and len(full_name) > 2:
            score += CompletenessScorer.WEIGHTS['full_name']

        # Score bio (length matters)
        if bio and len(bio) > 20:
            score += CompletenessScorer.WEIGHTS['bio']

        # Score license status
        if license_status:
            score += CompletenessScorer.WEIGHTS['license_status']

        # Score photo URL
        if photo_url:
            score += CompletenessScorer.WEIGHTS['photo_url']

        # Score bar number
        if bar_number:
            score += CompletenessScorer.WEIGHTS['bar_number']

        # Score city/state (parse from address)
        if address:
            # If we have an address, assume we have city/state
            # (proper parsing happens in address normalizer)
            score += CompletenessScorer.WEIGHTS['city']
            score += CompletenessScorer.WEIGHTS['state']

        # Score phone
        if phone:
            score += CompletenessScorer.WEIGHTS['phone']

        # Score law school
        if law_school:
            score += CompletenessScorer.WEIGHTS['law_school']

        # Score admission date/year
        if admission_year:
            score += CompletenessScorer.WEIGHTS['admission_date']

        # Score firm name
        if firm_name:
            score += CompletenessScorer.WEIGHTS['firm_name']

        # Score website
        if website_url:
            score += CompletenessScorer.WEIGHTS['website_url']

        # Score email
        if email:
            score += CompletenessScorer.WEIGHTS['email']

        return min(score, 100)
