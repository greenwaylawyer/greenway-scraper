"""AI-enhanced practice area normalizer.

Workflow:
1. Try the existing rule-based dictionary (free, instant).
2. For terms that couldn't be mapped, batch-classify with Claude Haiku.
3. Cache AI responses so the same term is never sent to the API twice.
4. If AI is disabled or the API call fails, silently skip unmapped terms.
"""

import asyncio
import json
from typing import Optional, Set

from ai import config as ai_config
from ai.cache import get_cache
from normalizers.practice_areas import PracticeAreaNormalizer
from utils.logger import get_logger

logger = get_logger(__name__)

_FEATURE = "practice_area_normalization"

# Canonical taxonomy list as a compact string for the prompt
_TAXONOMY = ", ".join(sorted(PracticeAreaNormalizer.CANONICAL_PRACTICE_AREAS))

_SYSTEM_PROMPT = """\
You are a legal practice area classifier. Your ONLY job is to map raw attorney \
practice area terms to the canonical taxonomy below.

Canonical taxonomy:
{taxonomy}

Rules:
- Return ONLY a JSON object mapping each input term to ONE canonical category.
- If a term maps to multiple categories, pick the most specific one.
- If a term genuinely does not fit any category, map it to null.
- No explanation, no markdown, just the JSON object.
""".format(taxonomy=_TAXONOMY)


class AIPracticeAreaNormalizer:
    """Practice area normalizer that extends rule-based logic with Claude Haiku."""

    def __init__(self):
        self._cache = get_cache()
        self._client = None   # Lazy init to avoid import errors when AI is disabled

    def _get_client(self):
        if self._client is None:
            from ai.providers.factory import get_client
            self._client = get_client(_FEATURE)
        return self._client

    def normalize(self, raw_areas: Optional[list]) -> Set[str]:
        """
        Synchronous wrapper — runs the async version in a new event loop if needed.
        Suitable for calling from non-async pipeline code.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                # We're already inside an async context — can't nest run().
                # Caller should use normalize_async() directly.
                return self._rule_based(raw_areas)
            return loop.run_until_complete(self.normalize_async(raw_areas))
        except RuntimeError:
            return asyncio.run(self.normalize_async(raw_areas))

    async def normalize_async(self, raw_areas: Optional[list]) -> Set[str]:
        """
        Full normalization: rules first, then AI for unmapped terms.
        """
        if not raw_areas:
            return set()

        # Step 1: rule-based pass
        mapped = self._rule_based(raw_areas)
        mapped_lower = {a.strip().lower() for a in raw_areas
                        if a and a.strip().lower() in PracticeAreaNormalizer.RAW_TO_CANONICAL
                        or a and a.strip().lower() in PracticeAreaNormalizer.CANONICAL_PRACTICE_AREAS}

        unmapped = [
            a for a in raw_areas
            if a and a.strip().lower() not in PracticeAreaNormalizer.RAW_TO_CANONICAL
            and a.strip().lower() not in PracticeAreaNormalizer.CANONICAL_PRACTICE_AREAS
        ]

        if not unmapped or not ai_config.is_feature_enabled(_FEATURE):
            return mapped

        # Step 2: AI pass for unmapped terms
        ai_results = await self._ai_classify(unmapped)
        for canonical in ai_results.values():
            if canonical and canonical in PracticeAreaNormalizer.CANONICAL_PRACTICE_AREAS:
                mapped.add(canonical)

        return mapped

    def _rule_based(self, raw_areas: Optional[list]) -> Set[str]:
        return PracticeAreaNormalizer.normalize(raw_areas)

    async def _ai_classify(self, terms: list) -> dict:
        """
        Classify a list of unmapped terms using Claude Haiku.
        Returns dict {raw_term: canonical_or_null}.
        """
        # Check cache first (one cache entry per batch)
        cache_key = sorted(t.strip().lower() for t in terms)
        cached = self._cache.get(_FEATURE, cache_key)
        if cached is not None:
            return cached

        batch_size = ai_config.feature_batch_size(_FEATURE)
        results: dict = {}

        # Process in batches
        for i in range(0, len(terms), batch_size):
            batch = terms[i:i + batch_size]
            batch_result = await self._classify_batch(batch)
            results.update(batch_result)

        # Cache the combined result
        self._cache.set(_FEATURE, cache_key, results)
        return results

    async def _classify_batch(self, terms: list) -> dict:
        """Send one batch of terms to Claude and parse the response."""
        user_msg = (
            "Map these raw practice area terms to the canonical taxonomy.\n"
            f"Terms: {json.dumps(terms)}\n"
            "Return JSON only."
        )
        try:
            client = self._get_client()
            result = await client.complete_json(
                system=_SYSTEM_PROMPT,
                user=user_msg,
                max_tokens=512,
            )
            if isinstance(result, dict):
                logger.info(
                    "AI practice area batch classified",
                    terms=len(terms),
                    mapped=sum(1 for v in result.values() if v),
                )
                return result
        except Exception as e:
            logger.warning(
                "AI practice area classification failed — skipping batch",
                error=str(e),
                terms=terms[:5],
            )
        return {}


# Module-level singleton
_instance: Optional[AIPracticeAreaNormalizer] = None


def get_normalizer() -> AIPracticeAreaNormalizer:
    global _instance
    if _instance is None:
        _instance = AIPracticeAreaNormalizer()
    return _instance
