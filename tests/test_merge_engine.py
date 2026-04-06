"""
Unit tests for MergeEngine.

Tests the 4-rule merge logic for combining scraped data.
"""

import pytest
from pipeline.merge_engine import MergeEngine, merge_scraped_data


def test_merge_new_fields():
    """Test merging new fields into empty profile."""
    engine = MergeEngine()
    
    current = {}
    scraped = {
        'bio': 'Test attorney',
        'practice_areas': ['Criminal', 'DUI'],
    }
    
    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='justia',
        layer=3,
        manually_curated_fields=[],
    )
    
    assert result['bio'] == 'Test attorney'
    assert result['practice_areas'] == ['Criminal', 'DUI']
    assert result['_field_sources']['bio'] == 'justia'


def test_merge_skip_curated_fields():
    """Test that manually curated fields are never overwritten."""
    engine = MergeEngine()
    
    current = {
        'bio': 'Manually curated bio',
        '_field_sources': {'bio': 'manual'},
    }
    scraped = {
        'bio': 'Scraped bio from source',
    }
    
    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='justia',
        layer=3,
        manually_curated_fields=['bio'],
    )
    
    # Bio should NOT change
    assert result['bio'] == 'Manually curated bio'
    assert 'bio' in engine.fields_skipped


def test_merge_detect_conflicts():
    """Test conflict detection when different sources provide different values."""
    engine = MergeEngine()
    
    current = {
        'practice_areas': ['Criminal Defense'],
        '_field_sources': {'practice_areas': 'justia'},
        '_conflicts': {},
        '_needs_review': [],
    }
    scraped = {
        'practice_areas': ['Criminal Defense', 'DUI', 'Traffic'],
    }
    
    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='avvo',
        layer=3,
        manually_curated_fields=[],
    )
    
    # Should flag conflict
    assert 'practice_areas' in result['_needs_review']
    assert 'practice_areas' in result['_conflicts']
    assert 'practice_areas' in engine.conflicts_detected


def test_merge_layer4_always_overwrites():
    """Test that Layer 4 data always overwrites (time-sensitive)."""
    engine = MergeEngine()
    
    current = {
        'google_rating': 4.5,
        'google_review_count': 10,
        '_field_sources': {
            'google_rating': 'google_maps',
            'google_review_count': 'google_maps',
        },
    }
    scraped = {
        'google_rating': 4.7,
        'google_review_count': 15,
    }
    
    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='google_maps',
        layer=4,
        manually_curated_fields=[],
    )
    
    # Should overwrite
    assert result['google_rating'] == 4.7
    assert result['google_review_count'] == 15


def test_completeness_score_calculation():
    """Test completeness score calculation."""
    engine = MergeEngine()
    
    merged_data = {
        'full_name': 'John Smith',       # 10 pts
        'bar_number': '123456',          # 8 pts
        'license_status': 'Active',      # 10 pts
        'practice_areas': ['Criminal'],  # 12 pts
        'bio': 'Test bio',               # 10 pts
        'phone': '555-1234',             # 6 pts
        'email': 'test@test.com',        # 2 pts
    }
    
    score = engine.calculate_completeness_score(merged_data)
    
    # Should be 58 points (10+8+10+12+10+6+2)
    assert score == 58
