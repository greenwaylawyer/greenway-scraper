"""Filtering, deduplication, and ranking for Google-first discovery."""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import re
from typing import Any, Dict, Iterable, List

from normalizers.name import NameNormalizer


ENTITY_SUFFIXES = (
    "llp",
    "llc",
    "pc",
    "p.c.",
    "pllc",
    "inc",
    "corp",
    "co.",
)

FIRM_KEYWORDS = (
    "law group",
    "legal services",
    "law office",
    "law offices",
    "attorneys at law",
    "associates",
    "partners",
    "trial lawyers",
    "injury lawyers",
)


@dataclass
class FilterResult:
    kept: List[Dict[str, Any]]
    removed: List[Dict[str, Any]]


def looks_like_firm_name(name: str) -> bool:
    lowered = (name or "").strip().lower()
    if not lowered:
        return True
    if any(lowered.endswith(suffix) for suffix in ENTITY_SUFFIXES):
        return True
    if any(keyword in lowered for keyword in FIRM_KEYWORDS):
        return True
    # Common firm shape: "A, B & C"
    if lowered.count("&") >= 1 and lowered.count(",") >= 1:
        return True
    # "Smith, Jones & Williams" style, but avoid normal "Last, First" names.
    if lowered.count("&") >= 1 and len(re.findall(r"\b[a-z]{3,}\b", lowered)) >= 3:
        return True
    return False


def looks_like_individual(name: str) -> bool:
    if looks_like_firm_name(name):
        return False
    parsed = NameNormalizer.parse(name)
    if not parsed:
        return False
    if not parsed.first_name or not parsed.last_name:
        return False
    # Require a minimum character length for both sides to avoid noise.
    return len(parsed.first_name) >= 2 and len(parsed.last_name) >= 2


def popularity_score(rating: float | None, review_count: int | None) -> float:
    r = float(rating or 0.0)
    reviews = int(review_count or 0)
    raw = (r * 20.0) + min(reviews * 0.3, 40.0)
    return min(100.0, max(0.0, (raw / 140.0) * 100.0))


def discovery_fingerprint(name: str, city: str, state: str) -> str:
    normalized = f"{(name or '').strip().lower()}|{(city or '').strip().lower()}|{(state or '').strip().lower()}"
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def filter_individuals(candidates: Iterable[Dict[str, Any]]) -> FilterResult:
    kept: List[Dict[str, Any]] = []
    removed: List[Dict[str, Any]] = []
    for candidate in candidates:
        name = candidate.get("name") or ""
        if looks_like_individual(name):
            kept.append(candidate)
        else:
            removed.append(candidate)
    return FilterResult(kept=kept, removed=removed)


def dedupe_and_rank(
    candidates: Iterable[Dict[str, Any]],
    state_quotas: Dict[str, int],
    min_rating: float = 3.5,
    min_review_count: int = 5,
    min_popularity_score: float = 40.0,
) -> List[Dict[str, Any]]:
    """Return selected, ranked candidates per state quota."""
    deduped: Dict[str, Dict[str, Any]] = {}
    for item in candidates:
        rating = float(item.get("google_rating") or 0.0)
        reviews = int(item.get("google_review_count") or 0)
        score = popularity_score(rating, reviews)
        item["google_popularity_score"] = round(score, 2)
        if rating < min_rating or reviews < min_review_count or score < min_popularity_score:
            continue
        fp = discovery_fingerprint(
            item.get("name", ""),
            item.get("city", ""),
            item.get("state", ""),
        )
        item["google_discovery_fingerprint"] = fp
        existing = deduped.get(fp)
        if existing is None or item["google_popularity_score"] > existing.get("google_popularity_score", 0):
            deduped[fp] = item

    by_state: Dict[str, List[Dict[str, Any]]] = {}
    for item in deduped.values():
        state = (item.get("state") or "").upper()
        by_state.setdefault(state, []).append(item)

    selected: List[Dict[str, Any]] = []
    for state, rows in by_state.items():
        rows.sort(key=lambda r: r.get("google_popularity_score", 0), reverse=True)
        quota = int(state_quotas.get(state, 0))
        if quota <= 0:
            continue
        for idx, row in enumerate(rows[:quota], start=1):
            row["google_discovery_rank"] = idx
            row["google_discovery_selected"] = True
            selected.append(row)
    return selected
