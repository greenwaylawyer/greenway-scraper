"""Tests for MVP publish gate validation."""

from pipeline.mvp_publish_gate import MVPPublishGate


def test_publish_gate_accepts_complete_profile():
    gate = MVPPublishGate()
    valid, reason = gate._validate(
        {
            "full_name": "John Michael Smith",
            "city": "Los Angeles",
            "state": "CA",
            "completeness_score": 75,
            "google_rating": 4.7,
            "raw_data_by_source": {"google_discovery": {}, "justia": {}},
            "merged_data": {"practice_areas": ["Personal Injury"]},
        }
    )
    assert valid is True
    assert reason is None


def test_publish_gate_rejects_missing_practice_areas():
    gate = MVPPublishGate()
    valid, reason = gate._validate(
        {
            "full_name": "John Smith",
            "city": "Los Angeles",
            "state": "CA",
            "completeness_score": 80,
            "google_rating": 4.2,
            "raw_data_by_source": {"google_discovery": {}},
            "merged_data": {"practice_areas": []},
        }
    )
    assert valid is False
    assert reason == "missing_practice_areas"
