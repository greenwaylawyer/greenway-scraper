"""AI configuration loader.

Reads config/ai.yaml and provides helper methods for checking
whether AI is enabled globally or for a specific feature.
"""

from pathlib import Path
from typing import Optional
import yaml

from utils.logger import get_logger

logger = get_logger(__name__)

_CONFIG: Optional[dict] = None


def _load() -> dict:
    global _CONFIG
    if _CONFIG is None:
        config_path = Path(__file__).parent.parent / "config" / "ai.yaml"
        if not config_path.exists():
            logger.warning("config/ai.yaml not found — AI disabled")
            _CONFIG = {"ai": {"enabled": False}}
        else:
            with open(config_path) as f:
                _CONFIG = yaml.safe_load(f) or {"ai": {"enabled": False}}
    return _CONFIG


def get() -> dict:
    """Return the full AI config dict (under the 'ai' key)."""
    return _load().get("ai", {})


def is_enabled() -> bool:
    """Master kill-switch check."""
    return bool(get().get("enabled", False))


def is_feature_enabled(feature: str) -> bool:
    """Check if a specific feature is enabled (requires master switch too)."""
    if not is_enabled():
        return False
    features = get().get("features", {})
    return bool(features.get(feature, {}).get("enabled", False))


def feature_model(feature: str) -> str:
    """Return the model name for a specific feature."""
    features = get().get("features", {})
    return features.get(feature, {}).get("model", get().get("model", "claude-haiku-3-5-20241022"))


def feature_batch_size(feature: str) -> int:
    """Return the batch size for a specific feature."""
    features = get().get("features", {})
    return int(features.get(feature, {}).get("batch_size", 50))


def cache_config() -> dict:
    return get().get("cache", {"enabled": False})


def rate_limit_config() -> dict:
    return get().get("rate_limits", {})
