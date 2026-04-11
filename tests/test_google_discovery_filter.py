"""Tests for Google-first discovery filtering and ranking."""

from pipeline.google_discovery_filter import (
    dedupe_and_rank,
    discovery_fingerprint,
    filter_individuals,
    looks_like_individual,
    popularity_score,
)


def test_individual_name_detection():
    assert looks_like_individual("John Smith")
    assert looks_like_individual("Robert K. Johnson")
    assert not looks_like_individual("Smith & Associates LLP")
    assert not looks_like_individual("Metro Legal Services")


def test_popularity_score_bounds():
    assert popularity_score(0.0, 0) == 0.0
    assert 0 < popularity_score(4.8, 100) <= 100


def test_discovery_fingerprint_is_stable():
    left = discovery_fingerprint("John Smith", "Los Angeles", "CA")
    right = discovery_fingerprint("john smith", "los angeles", "ca")
    assert left == right


def test_filter_individuals_splits_lists():
    rows = [
        {"name": "John Smith"},
        {"name": "Smith Law Group"},
    ]
    result = filter_individuals(rows)
    assert len(result.kept) == 1
    assert len(result.removed) == 1


def test_dedupe_and_rank_by_state_quota():
    rows = [
        {
            "name": "John Smith",
            "city": "Los Angeles",
            "state": "CA",
            "google_rating": 4.9,
            "google_review_count": 100,
        },
        # Duplicate fingerprint with lower score should be dropped.
        {
            "name": "John Smith",
            "city": "Los Angeles",
            "state": "CA",
            "google_rating": 4.0,
            "google_review_count": 10,
        },
        {
            "name": "Jane Doe",
            "city": "San Diego",
            "state": "CA",
            "google_rating": 4.7,
            "google_review_count": 80,
        },
    ]
    selected = dedupe_and_rank(rows, state_quotas={"CA": 1})
    assert len(selected) == 1
    assert selected[0]["google_discovery_rank"] == 1
    assert selected[0]["google_discovery_selected"] is True
