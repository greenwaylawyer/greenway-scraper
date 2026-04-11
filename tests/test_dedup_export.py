"""Tests for bar_number normalization and export deduplication."""

from scrapers.base import LawyerRawData
from pipeline.dedup import normalize_bar_number, FingerprintGenerator
from pipeline.exporter import DataExporter


def test_normalize_bar_number_equivalence():
    assert normalize_bar_number(" 123456 ") == "123456"
    assert normalize_bar_number("#123456") == "123456"
    assert normalize_bar_number("# 123456 ") == "123456"
    assert normalize_bar_number("AB-12") == "ab-12"


def test_fingerprint_same_for_equivalent_bars():
    a = LawyerRawData(full_name="Jane Doe", bar_number=" 123456 ")
    b = LawyerRawData(full_name="Jane Doe", bar_number="#123456")
    assert FingerprintGenerator.generate(a, "CA") == FingerprintGenerator.generate(b, "CA")


def test_dedupe_records_keeps_higher_score():
    ex = DataExporter(state="CA", source="calbar")
    r1 = {
        "bar_number": "100",
        "license_state": "CA",
        "completeness_score": 10,
        "fingerprint": "a",
    }
    r2 = {
        "bar_number": "#100",
        "license_state": "CA",
        "completeness_score": 50,
        "fingerprint": "b",
    }
    out = ex._dedupe_records_by_bar_and_state([r1, r2])
    assert len(out) == 1
    assert out[0]["completeness_score"] == 50


def test_dedupe_skips_empty_bar():
    ex = DataExporter(state="CA", source="calbar")
    r1 = {"bar_number": None, "license_state": "CA", "completeness_score": 1}
    r2 = {"bar_number": "", "license_state": "CA", "completeness_score": 2}
    out = ex._dedupe_records_by_bar_and_state([r1, r2])
    assert len(out) == 2
