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
    assert any('bio' in s for s in engine.fields_skipped)


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
        'full_name': 'John Smith',       # 15 pts (Tier 1)
        'bar_number': '123456',          # 15 pts (Tier 1)
        'license_status': 'Active',      # not scored (not in tiers)
        'practice_areas': ['Criminal'],  # 10 pts (Tier 2)
        'bio': 'Test bio',               # 10 pts (Tier 2)
        'phone': '555-1234',             # 5 pts (Tier 3)
        'email': 'test@test.com',        # 5 pts (Tier 3)
    }

    score = engine.calculate_completeness_score(merged_data)

    # Should be 60 points (15+15+10+10+5+5)
    assert score == 60


# ── Identity fill-if-empty (Manual Scraper rows seeded with a title) ──────────


def test_identity_fill_if_empty_writes_once():
    """A Layer-3 source may populate identity ONCE when empty (title-seeded row)."""
    engine = MergeEngine()

    # Row created by Manual Scraper: no real name yet, only an admin_title note.
    current = {'admin_title': 'John Smith — NY Immigration'}
    scraped = {'full_name': 'John Smith', 'first_name': 'John', 'last_name': 'Smith'}

    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='justia',
        layer=3,
        manually_curated_fields=[],
    )

    assert result['full_name'] == 'John Smith'
    assert result['first_name'] == 'John'
    assert result['last_name'] == 'Smith'
    assert result['_field_sources']['full_name'] == 'justia'


def test_identity_fill_blocked_when_already_populated():
    """After the first write, identity protection resumes — no overwrite."""
    engine = MergeEngine()

    current = {
        'full_name': 'John Smith',
        'first_name': 'John',
        'last_name': 'Smith',
        '_field_sources': {'full_name': 'justia', 'first_name': 'justia', 'last_name': 'justia'},
    }
    scraped = {'full_name': 'Jonathan Smith', 'first_name': 'Jonathan'}

    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='avvo',
        layer=3,
        manually_curated_fields=[],
    )

    # Identity stays — not overwritten by a second source.
    assert result['full_name'] == 'John Smith'
    assert result['first_name'] == 'John'


def test_identity_curated_always_wins():
    """Manually curated identity is never overwritten, even when empty is intended."""
    engine = MergeEngine()

    current = {'full_name': ''}  # intentionally blanked by admin
    scraped = {'full_name': 'Scraped Name'}

    result = engine.merge(
        current_merged_data=current,
        scraped_data=scraped,
        source_key='avvo',
        layer=3,
        manually_curated_fields=['full_name'],  # admin curated it
    )

    assert result.get('full_name', '') == ''
    assert any('full_name' in s for s in engine.fields_skipped)


# ── Name split normalization ─────────────────────────────────────────────────


def test_name_split_from_full_name():
    """A source emitting only full_name is split into first/last."""
    from pipeline.merge_engine import normalize_name_split

    data = {'full_name': 'John Smith'}
    normalize_name_split(data)
    assert data['first_name'] == 'John'
    assert data['last_name'] == 'Smith'


def test_name_split_multi_word_first():
    """Multi-word first names split on the last token."""
    from pipeline.merge_engine import normalize_name_split

    data = {'full_name': 'Mary Jane Watson'}
    normalize_name_split(data)
    assert data['first_name'] == 'Mary Jane'
    assert data['last_name'] == 'Watson'


def test_name_compose_from_first_last():
    """first/last without full_name composes full_name."""
    from pipeline.merge_engine import normalize_name_split

    data = {'first_name': 'Jane', 'last_name': 'Doe'}
    normalize_name_split(data)
    assert data['full_name'] == 'Jane Doe'


def test_name_split_skips_single_token():
    """A single-token full_name is not split (ambiguous)."""
    from pipeline.merge_engine import normalize_name_split

    data = {'full_name': 'Madonna'}
    normalize_name_split(data)
    assert 'first_name' not in data
    assert 'last_name' not in data
