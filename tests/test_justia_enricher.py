"""
Unit tests for JustiaEnricher.

TODO: Implement tests after scraper is complete.

Test coverage should include:
1. Discovery with name+city search
2. Discovery with bar number search
3. Candidate scoring logic
4. Profile parsing with fixture HTML
5. Error handling (404, timeout, invalid HTML)
"""

import pytest
import asyncio
from pathlib import Path

# TODO: Uncomment when enricher is implemented
# from scrapers.enrichers.justia import JustiaEnricher


@pytest.mark.asyncio
async def test_justia_discovery_stub():
    """Stub test for Justia discovery."""
    # TODO: Implement after enricher is complete
    assert True, "Justia enricher not yet implemented"


@pytest.mark.asyncio
async def test_justia_parse_profile_stub():
    """Stub test for Justia profile parsing."""
    # TODO: Load fixture HTML from tests/fixtures/justia/sample_profile.html
    # TODO: Test parsing logic
    assert True, "Justia enricher not yet implemented"


def test_justia_confidence_scoring_stub():
    """Stub test for candidate confidence scoring."""
    # TODO: Test scoring algorithm with various match scenarios
    assert True, "Justia enricher not yet implemented"
