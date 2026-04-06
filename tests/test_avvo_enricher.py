"""
Unit tests for AvvoEnricher.

TODO: Implement tests after scraper is complete.

Test coverage should include:
1. Discovery with name+city search
2. Discovery with phone search
3. Candidate scoring logic
4. Profile parsing with fixture HTML
5. Work history parsing
6. Professional associations parsing
7. Error handling
"""

import pytest
import asyncio
from pathlib import Path

# TODO: Uncomment when enricher is implemented
# from scrapers.enrichers.avvo import AvvoEnricher


@pytest.mark.asyncio
async def test_avvo_discovery_stub():
    """Stub test for Avvo discovery."""
    assert True, "Avvo enricher not yet implemented"


@pytest.mark.asyncio
async def test_avvo_parse_profile_stub():
    """Stub test for Avvo profile parsing."""
    assert True, "Avvo enricher not yet implemented"


def test_avvo_work_history_parsing_stub():
    """Stub test for work history parsing."""
    assert True, "Avvo enricher not yet implemented"
