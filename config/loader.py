"""Configuration loader for enrichment sources."""

import os
from pathlib import Path
from typing import Dict, Any, Optional, List
import yaml


def get_config_path() -> Path:
    """Get the path to the config directory."""
    return Path(__file__).parent


def load_enrichment_sources() -> Dict[str, Any]:
    """
    Load enrichment source configuration from YAML.
    
    Returns:
        Dictionary with source configurations keyed by source_key.
    """
    config_file = get_config_path() / 'enrichment_sources.yaml'
    
    with open(config_file, 'r') as f:
        config = yaml.safe_load(f)
    
    return config.get('sources', {})


def get_source_config(source_key: str) -> Optional[Dict[str, Any]]:
    """
    Get configuration for a specific source.
    
    Args:
        source_key: The source identifier (e.g., 'justia', 'avvo', 'google_maps')
    
    Returns:
        Source configuration dict or None if not found.
    """
    sources = load_enrichment_sources()
    return sources.get(source_key)


def get_enabled_sources(layer: Optional[int] = None) -> Dict[str, Dict[str, Any]]:
    """
    Get all enabled sources, optionally filtered by layer.
    
    Args:
        layer: Optional layer number (3 or 4) to filter by.
    
    Returns:
        Dictionary of enabled source configurations.
    """
    sources = load_enrichment_sources()
    enabled = {
        key: config
        for key, config in sources.items()
        if config.get('enabled', False)
    }
    
    if layer is not None:
        enabled = {
            key: config
            for key, config in enabled.items()
            if config.get('layer') == layer
        }
    
    return enabled


def get_sources_by_layer(layer: int) -> Dict[str, Dict[str, Any]]:
    """
    Get all sources for a specific layer (enabled or not).
    
    Args:
        layer: Layer number (3 or 4).
    
    Returns:
        Dictionary of source configurations for that layer.
    """
    sources = load_enrichment_sources()
    return {
        key: config
        for key, config in sources.items()
        if config.get('layer') == layer
    }


def get_source_rate_limit(source_key: str) -> int:
    """
    Get rate limit (requests per minute) for a source.
    
    Args:
        source_key: The source identifier.
    
    Returns:
        Rate limit in requests per minute (default 10 if not specified).
    """
    config = get_source_config(source_key)
    if not config:
        return 10  # default
    return config.get('rate_limit', 10)


def get_discovery_strategies(source_key: str) -> List[str]:
    """
    Get discovery strategies for a source.
    
    Args:
        source_key: The source identifier.
    
    Returns:
        List of strategy names.
    """
    config = get_source_config(source_key)
    if not config:
        return []
    discovery = config.get('discovery', {})
    return discovery.get('strategies', [])


def get_confidence_threshold(source_key: str) -> float:
    """
    Get auto-merge confidence threshold for a source.
    
    Args:
        source_key: The source identifier.
    
    Returns:
        Confidence threshold (0.0-1.0, default 0.90).
    """
    config = get_source_config(source_key)
    if not config:
        return 0.90
    discovery = config.get('discovery', {})
    return discovery.get('confidence_threshold', 0.90)


if __name__ == '__main__':
    # Test the loader
    print("All sources:")
    sources = load_enrichment_sources()
    for key, config in sources.items():
        print(f"  {key}: {config['name']} (Layer {config['layer']}, "
              f"enabled={config.get('enabled', False)})")
    
    print("\nEnabled sources:")
    enabled = get_enabled_sources()
    print(f"  Count: {len(enabled)}")
    
    print("\nLayer 3 sources:")
    layer3 = get_sources_by_layer(3)
    for key in layer3:
        print(f"  {key}")
    
    print("\nLayer 4 sources:")
    layer4 = get_sources_by_layer(4)
    for key in layer4:
        print(f"  {key}")
