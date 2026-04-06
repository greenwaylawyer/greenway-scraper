"""
Unit tests for GooglePlacesEnricher.

TODO: Implement tests with mocked API responses.

Test coverage should include:
1. Find Place API call with mocked response
2. Place Details API call with mocked response
3. Address/phone matching logic
4. Confidence scoring
5. Cost verification (no real API calls in tests)
6. place_id reuse logic
"""

import pytest
import asyncio
from unittest.mock import patch, MagicMock

# TODO: Uncomment when enricher is implemented
# from scrapers.enrichers.google_places import GooglePlacesEnricher


@pytest.mark.asyncio
async def test_google_places_discovery_mock_stub():
    """Stub test for Google Places discovery with mocked API."""
    # TODO: Mock aiohttp response
    # TODO: Verify no real API calls made
    assert True, "Google Places enricher not yet implemented"


@pytest.mark.asyncio
async def test_google_places_details_mock_stub():
    """Stub test for Google Places details with mocked API."""
    assert True, "Google Places enricher not yet implemented"


def test_google_places_cost_tracking_stub():
    """Stub test to verify cost tracking."""
    # TODO: Count API calls and calculate cost
    assert True, "Google Places enricher not yet implemented"
