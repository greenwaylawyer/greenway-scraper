"""
Merge engine for integrating scraped data into lawyer_enrichment profiles.

Implements the 4-rule merge strategy:
1. Skip manually curated fields (never overwrite)
2. Protect identity fields (Layer 1-2 authority only)
3. Merge Layer 3 enrichment fields (detect conflicts)
4. Layer 4 fields always overwrite (time-sensitive)
"""

from typing import Dict, Any, List, Set, Optional
from copy import deepcopy
import logging

logger = logging.getLogger(__name__)


# Identity fields protected by Rule 2
IDENTITY_FIELDS = {
    'full_name',
    'first_name',
    'last_name',
    'bar_number',
    'license_status',
    'admission_date',
    'license_state',
}

# Layer 4 fields that always overwrite (Rule 4)
LAYER_4_FIELDS = {
    'google_rating',
    'google_review_count',
    'google_reviews',
    'google_photos',
    'place_id',
    'google_maps_url',
    'formatted_phone_number',  # from Google as verification/gap-fill
}

# Layer 1-2 official source keys
OFFICIAL_SOURCES = {
    'calbar',
    'calbar_detail',
    'nybar',
    'nybar_detail',
    'ncbar',
    'ncbar_detail',
    'state_bars',  # generic Layer 1
}


class MergeEngine:
    """Handles merging of scraped data into lawyer_enrichment profiles."""

    def __init__(self):
        self.conflicts_detected = []
        self.fields_merged = []
        self.fields_skipped = []

    def merge(
        self,
        current_merged_data: Dict[str, Any],
        scraped_data: Dict[str, Any],
        source_key: str,
        layer: int,
        manually_curated_fields: List[str],
    ) -> Dict[str, Any]:
        """
        Merge scraped data into existing merged_data.

        Args:
            current_merged_data: Existing merged_data from lawyer_enrichment
            scraped_data: Newly scraped data from this source
            source_key: Source identifier (e.g., 'justia', 'avvo', 'google_maps')
            layer: Enrichment layer (3 or 4)
            manually_curated_fields: List of field names locked by admin

        Returns:
            Updated merged_data dict
        """
        self.conflicts_detected = []
        self.fields_merged = []
        self.fields_skipped = []

        # Deep copy to avoid mutating input
        updated_data = deepcopy(current_merged_data)

        # Ensure metadata keys exist
        if '_field_sources' not in updated_data:
            updated_data['_field_sources'] = {}
        if '_conflicts' not in updated_data:
            updated_data['_conflicts'] = {}
        if '_needs_review' not in updated_data:
            updated_data['_needs_review'] = []

        for field, value in scraped_data.items():
            # Skip empty/None values
            if value is None or value == '' or (isinstance(value, list) and len(value) == 0):
                continue

            # RULE 1: Skip manually curated fields
            if field in manually_curated_fields:
                logger.debug(f"Skipping curated field: {field}")
                self.fields_skipped.append(f"{field} (curated)")
                continue

            # RULE 2: Protect identity fields (Layers 1-2 only can write)
            if field in IDENTITY_FIELDS and layer >= 3:
                logger.debug(f"Skipping identity field from Layer {layer}: {field}")
                self.fields_skipped.append(f"{field} (identity)")
                continue

            # RULE 4: Layer 4 fields always overwrite
            if field in LAYER_4_FIELDS and layer == 4:
                updated_data[field] = value
                updated_data['_field_sources'][field] = source_key
                self.fields_merged.append(field)
                logger.debug(f"Layer 4 overwrite: {field}")
                continue

            # RULE 3: Layer 3 enrichment fields — detect conflicts
            existing_value = updated_data.get(field)
            existing_source = updated_data['_field_sources'].get(field)

            if existing_value is None:
                # Field not yet populated — write it
                updated_data[field] = value
                updated_data['_field_sources'][field] = source_key
                self.fields_merged.append(field)
                logger.debug(f"New field: {field} from {source_key}")

            elif existing_source == source_key:
                # Same source is refreshing its own data — overwrite
                updated_data[field] = value
                updated_data['_field_sources'][field] = source_key
                self.fields_merged.append(f"{field} (refresh)")
                logger.debug(f"Refresh field: {field} from {source_key}")

            else:
                # Conflict: different source already provided this field
                if not self._are_values_equal(existing_value, value):
                    logger.warning(
                        f"Conflict detected for {field}: "
                        f"{existing_source}={existing_value} vs {source_key}={value}"
                    )
                    
                    # Store conflict
                    if field not in updated_data['_conflicts']:
                        updated_data['_conflicts'][field] = {}
                    
                    updated_data['_conflicts'][field][existing_source] = existing_value
                    updated_data['_conflicts'][field][source_key] = value
                    
                    # Flag for review
                    if field not in updated_data['_needs_review']:
                        updated_data['_needs_review'].append(field)
                    
                    self.conflicts_detected.append(field)
                    self.fields_skipped.append(f"{field} (conflict)")
                else:
                    # Values are identical — no conflict, just log
                    logger.debug(f"Same value from different sources: {field}")
                    self.fields_merged.append(f"{field} (duplicate)")

        return updated_data

    @staticmethod
    def _are_values_equal(val1: Any, val2: Any) -> bool:
        """
        Compare two values for equality, handling lists/dicts.
        
        For lists, consider them equal if they have significant overlap
        (not exact match required, since sources may format differently).
        """
        if type(val1) != type(val2):
            return False

        if isinstance(val1, list) and isinstance(val2, list):
            # Convert to sets for comparison (order-independent)
            set1 = set(str(x).lower() for x in val1)
            set2 = set(str(x).lower() for x in val2)
            overlap = len(set1 & set2)
            total = len(set1 | set2)
            # Consider equal if >80% overlap
            return (overlap / total) >= 0.8 if total > 0 else False

        if isinstance(val1, dict) and isinstance(val2, dict):
            # Dicts are equal if they have the same keys and values
            return val1 == val2

        # Simple comparison for strings, numbers, etc.
        return str(val1).lower().strip() == str(val2).lower().strip()

    def calculate_completeness_score(self, merged_data: Dict[str, Any]) -> int:
        """
        Calculate completeness score (0-100) using a tiered model.

        Scoring tiers                 Points each   Max
        ─────────────────────────────────────────────────
        Tier 1 – Must have  × 4       15            60
          bar_number, license_state, full_name, address
        Tier 2 – High value × 3       10            30
          practice_areas, bio, photo_url
        Tier 3 – Medium     × 2        5            10
          phone, email
        ─────────────────────────────────────────────────
        TOTAL                                       100

        Optional fields (education, work history, professional associations,
        publications, languages, websites, firm_name, etc.) are intentionally
        excluded from scoring to avoid inflating completeness with supplementary
        data that many lawyers will never have.

        Args:
            merged_data: The profile's merged_data dict

        Returns:
            Integer score from 0 to 100
        """
        def _has(key: str) -> bool:
            v = merged_data.get(key)
            if v is None or v == "" or v == "None":
                return False
            if isinstance(v, list):
                return len(v) > 0
            if isinstance(v, dict):
                return any(bool(vv) for vv in v.values())
            return True

        score = 0

        # ── Tier 1: Must-have (15 pts each = 60 max) ─────────────────────────
        if _has("bar_number"):
            score += 15

        if _has("license_state"):
            score += 15

        if _has("full_name"):
            score += 15

        # Address: accept a populated address dict, a top-level city, or a state
        address_obj = merged_data.get("address")
        has_address = (
            isinstance(address_obj, dict)
            and any(address_obj.get(f) for f in ("city", "zip", "line1"))
        ) or _has("city") or _has("state")
        if has_address:
            score += 15

        # ── Tier 2: High-value (10 pts each = 30 max) ────────────────────────
        if _has("practice_areas"):
            score += 10

        if _has("bio"):
            score += 10

        if _has("photo_url"):
            score += 10

        # ── Tier 3: Medium (5 pts each = 10 max) ─────────────────────────────
        if _has("phone"):
            score += 5

        if _has("email"):
            score += 5

        return min(score, 100)

    @staticmethod
    def score_breakdown(merged_data: Dict[str, Any]) -> Dict[str, Any]:
        """
        Return a human-readable breakdown of the completeness score for debugging.

        Returns a dict with each field, its tier, weight, and whether it's present.
        """
        engine = MergeEngine()
        score = engine.calculate_completeness_score(merged_data)

        def _has(key):
            v = merged_data.get(key)
            if v is None or v == "" or v == "None":
                return False
            if isinstance(v, (list, dict)):
                return len(v) > 0
            return True

        address_obj = merged_data.get("address")
        has_address = (
            isinstance(address_obj, dict)
            and any(address_obj.get(f) for f in ("city", "zip", "line1"))
        ) or _has("city") or _has("state")

        fields = [
            {"field": "bar_number",    "tier": "must",   "weight": 15, "present": _has("bar_number")},
            {"field": "license_state", "tier": "must",   "weight": 15, "present": _has("license_state")},
            {"field": "full_name",     "tier": "must",   "weight": 15, "present": _has("full_name")},
            {"field": "address",       "tier": "must",   "weight": 15, "present": has_address},
            {"field": "practice_areas","tier": "high",   "weight": 10, "present": _has("practice_areas")},
            {"field": "bio",           "tier": "high",   "weight": 10, "present": _has("bio")},
            {"field": "photo_url",     "tier": "high",   "weight": 10, "present": _has("photo_url")},
            {"field": "phone",         "tier": "medium", "weight":  5, "present": _has("phone")},
            {"field": "email",         "tier": "medium", "weight":  5, "present": _has("email")},
        ]

        earned = sum(f["weight"] for f in fields if f["present"])
        return {"total_score": score, "earned": earned, "max": 100, "fields": fields}

    def get_merge_summary(self) -> Dict[str, Any]:
        """
        Get a summary of the merge operation.
        
        Returns:
            Dict with fields_merged, conflicts_detected, fields_skipped counts
        """
        return {
            'fields_merged': len(self.fields_merged),
            'fields_merged_list': self.fields_merged,
            'conflicts_detected': len(self.conflicts_detected),
            'conflicts_list': self.conflicts_detected,
            'fields_skipped': len(self.fields_skipped),
            'fields_skipped_list': self.fields_skipped,
        }


def merge_scraped_data(
    lawyer_enrichment: Dict[str, Any],
    scraped_data: Dict[str, Any],
    source_key: str,
    layer: int,
) -> Dict[str, Any]:
    """
    Convenience function to merge scraped data into a lawyer enrichment record.
    
    Args:
        lawyer_enrichment: Full lawyer_enrichment record dict
        scraped_data: Newly scraped data from source
        source_key: Source identifier
        layer: Enrichment layer (3 or 4)
    
    Returns:
        Dict with updated fields:
        {
            'merged_data': updated merged_data dict,
            'completeness_score': new score,
            'merge_summary': summary of what was merged/skipped/conflicted
        }
    """
    engine = MergeEngine()
    
    current_merged = lawyer_enrichment.get('merged_data', {})
    curated_fields = lawyer_enrichment.get('manually_curated_fields', [])
    
    updated_merged = engine.merge(
        current_merged_data=current_merged,
        scraped_data=scraped_data,
        source_key=source_key,
        layer=layer,
        manually_curated_fields=curated_fields,
    )
    
    new_score = engine.calculate_completeness_score(updated_merged)
    
    return {
        'merged_data': updated_merged,
        'completeness_score': new_score,
        'merge_summary': engine.get_merge_summary(),
    }


if __name__ == '__main__':
    # Test the merge engine
    import json
    
    # Sample existing data
    existing = {
        'full_name': 'John Smith',
        'bar_number': '123456',
        'practice_areas': ['Criminal Defense', 'DUI'],
        'bio': 'Attorney since 2005...',
        '_field_sources': {
            'full_name': 'calbar',
            'bar_number': 'calbar',
            'practice_areas': 'calbar_detail',
            'bio': 'calbar_detail',
        },
        '_conflicts': {},
        '_needs_review': [],
    }
    
    # New scraped data from Justia
    justia_data = {
        'bio': 'Experienced criminal defense attorney...',  # different
        'practice_areas': ['Criminal Defense', 'DUI', 'Traffic'],  # conflict
        'education_history': [{'school': 'UCLA', 'year': 2002}],  # new field
        'photo_url': 'https://justia.com/photo.jpg',  # new field
    }
    
    engine = MergeEngine()
    result = engine.merge(
        current_merged_data=existing,
        scraped_data=justia_data,
        source_key='justia',
        layer=3,
        manually_curated_fields=[],
    )
    
    print("Merge result:")
    print(json.dumps(result, indent=2))
    print("\nMerge summary:")
    print(json.dumps(engine.get_merge_summary(), indent=2))
