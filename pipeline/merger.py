"""Data merger for combining lawyer data from multiple enrichment sources."""

from typing import Any, Optional
from utils.logger import get_logger
from config.databases import get_enrichment_config

logger = get_logger(__name__)


class DataMerger:
    """Merge data from multiple sources using configured priority rules."""

    def __init__(self):
        """Initialize merger with enrichment configuration."""
        self.config = get_enrichment_config()
        self.merge_strategy = self.config.get_merge_strategy()

    def merge_lawyer_data(
        self,
        existing_data: dict,
        new_data: dict,
        source_name: str,
        existing_sources: list[str]
    ) -> tuple[dict, bool]:
        """
        Merge new lawyer data into existing data based on source priority.

        Args:
            existing_data: Current merged data
            new_data: New data from enrichment source
            source_name: Name of the new source (e.g., 'justia', 'state_bars')
            existing_sources: List of sources already merged

        Returns:
            Tuple of (merged_data, was_updated)
        """
        merged = existing_data.copy()
        was_updated = False

        for field, new_value in new_data.items():
            # Skip if new value is None or empty
            if new_value is None or (isinstance(new_value, str) and not new_value.strip()):
                continue

            # Get field merge strategy
            field_priority = self.merge_strategy.get(field, [source_name])

            # Special case: merge_all means combine all values (for lists)
            if field_priority == 'merge_all':
                merged_value = self._merge_lists(
                    merged.get(field, []),
                    new_value
                )
                if merged_value != merged.get(field):
                    merged[field] = merged_value
                    was_updated = True
                continue

            # Check if we should override existing value
            current_value = merged.get(field)
            current_source = merged.get(f'{field}_source')

            should_update = self._should_override(
                current_value=current_value,
                current_source=current_source,
                new_source=source_name,
                field_priority=field_priority
            )

            if should_update:
                merged[field] = new_value
                merged[f'{field}_source'] = source_name
                was_updated = True

                logger.debug(
                    "Field updated",
                    field=field,
                    old_value=current_value,
                    new_value=new_value,
                    new_source=source_name
                )

        return merged, was_updated

    def _should_override(
        self,
        current_value: Any,
        current_source: Optional[str],
        new_source: str,
        field_priority: list[str]
    ) -> bool:
        """
        Determine if new value should override existing value.

        Args:
            current_value: Existing field value
            current_source: Source of existing value
            new_source: Source of new value
            field_priority: Ordered list of source priorities (first = highest)

        Returns:
            True if should override, False otherwise
        """
        # If no existing value, always accept new value
        if current_value is None or (isinstance(current_value, str) and not current_value.strip()):
            return True

        # If no current source recorded, accept if no better source could come
        if current_source is None:
            return True

        # Check source priority
        try:
            current_priority_index = field_priority.index(current_source)
        except ValueError:
            # Current source not in priority list, accept new one
            current_priority_index = 999

        try:
            new_priority_index = field_priority.index(new_source)
        except ValueError:
            # New source not in priority list, don't override
            return False

        # Lower index = higher priority
        return new_priority_index < current_priority_index

    def _merge_lists(self, existing: list, new: Any) -> list:
        """
        Merge lists, removing duplicates.

        Args:
            existing: Existing list
            new: New value (can be list or single item)

        Returns:
            Merged list with duplicates removed
        """
        if not isinstance(existing, list):
            existing = []

        if not isinstance(new, list):
            new = [new] if new else []

        # Combine and deduplicate (case-insensitive for strings)
        combined = existing + new

        # Deduplicate
        seen = set()
        result = []
        for item in combined:
            # Normalize for comparison
            normalized = item.lower().strip() if isinstance(item, str) else item
            if normalized not in seen:
                seen.add(normalized)
                result.append(item)

        return result

    def calculate_completeness(self, merged_data: dict) -> int:
        """
        Calculate completeness score (0-100) based on configured weights.

        Args:
            merged_data: Merged lawyer data

        Returns:
            Completeness score (0-100)
        """
        weights = self.config.get_completeness_weights()
        total_weight = sum(weights.values())
        earned_score = 0

        for field, weight in weights.items():
            value = merged_data.get(field)

            # Check if field has a value
            has_value = False
            if value is not None:
                if isinstance(value, str) and value.strip():
                    has_value = True
                elif isinstance(value, list) and len(value) > 0:
                    has_value = True
                elif isinstance(value, (int, float, bool)):
                    has_value = True

            if has_value:
                earned_score += weight

        # Normalize to 0-100
        if total_weight > 0:
            return int((earned_score / total_weight) * 100)
        return 0

    def check_promotion_eligibility(
        self,
        merged_data: dict,
        completeness_score: int,
        enrichment_layers: list[str]
    ) -> tuple[bool, Optional[str]]:
        """
        Check if a lawyer profile is eligible for promotion to production.

        Args:
            merged_data: Merged lawyer data
            completeness_score: Current completeness score
            enrichment_layers: List of enrichment layers applied

        Returns:
            Tuple of (is_eligible, blocking_reason)
        """
        promotion_rules = self.config.get_promotion_rules()
        mandatory_fields = self.config.get_mandatory_fields()
        min_completeness = promotion_rules.get('min_completeness', 80)

        # Check completeness threshold
        if completeness_score < min_completeness:
            return False, f"Completeness {completeness_score}% below threshold {min_completeness}%"

        # Check mandatory fields
        for field in mandatory_fields:
            value = merged_data.get(field)
            if not value or (isinstance(value, str) and not value.strip()):
                return False, f"Missing mandatory field: {field}"

        # Check for blocking conditions
        block_conditions = promotion_rules.get('block_if', [])

        if 'duplicate_detected' in block_conditions:
            if merged_data.get('is_duplicate'):
                return False, "Duplicate lawyer detected"

        if 'multiple_bar_numbers_detected' in block_conditions:
            bar_numbers = merged_data.get('bar_numbers_found', [])
            if isinstance(bar_numbers, list) and len(bar_numbers) > 1:
                return False, "Multiple bar numbers detected"

        if 'conflicting_license_status' in block_conditions:
            if merged_data.get('license_status_conflict'):
                return False, "Conflicting license status from sources"

        # All checks passed
        return True, None
