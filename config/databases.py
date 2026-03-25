"""Database configuration for staging and production databases."""

import os
from dataclasses import dataclass
from typing import Optional
import yaml
from pathlib import Path


@dataclass
class DatabaseConfig:
    """Database connection configuration."""

    host: str
    port: int
    database: str
    user: str
    password: str

    def get_connection_string(self) -> str:
        """Get PostgreSQL connection string."""
        return f"host={self.host} port={self.port} dbname={self.database} user={self.user} password={self.password}"


class EnrichmentConfig:
    """Configuration for enrichment pipeline."""

    def __init__(self, config_path: Optional[str] = None):
        """Load enrichment configuration from YAML file."""
        if config_path is None:
            config_path = Path(__file__).parent.parent / "config" / "enrichment.yaml"

        with open(config_path) as f:
            # Replace environment variables in YAML
            content = f.read()
            for key, value in os.environ.items():
                content = content.replace(f"${{{key}}}", value)
                content = content.replace(f"${{{key}:-", "${").replace("}", "}")

            self.config = yaml.safe_load(content)

    def get_completeness_threshold(self) -> int:
        """Get minimum completeness score for promotion."""
        return self.config.get('completeness_threshold', 80)

    def get_scraper_db_config(self) -> DatabaseConfig:
        """Get scraper database configuration (enrichment pipeline)."""
        scraper = self.config.get('scraper_database', {})
        return DatabaseConfig(
            host=os.getenv('SCRAPER_DB_HOST', scraper.get('host', '127.0.0.1')),
            port=int(os.getenv('SCRAPER_DB_PORT', scraper.get('port', 5433))),
            database=os.getenv('SCRAPER_DB_NAME', scraper.get('database', 'greenway_scraper')),
            user=os.getenv('SCRAPER_DB_USER', scraper.get('user', 'scraper')),
            password=os.getenv('SCRAPER_DB_PASSWORD', scraper.get('password', 'secret')),
        )

    def get_portal_db_config(self) -> DatabaseConfig:
        """Get portal database configuration (Laravel public application)."""
        portal = self.config.get('portal_database', {})
        return DatabaseConfig(
            host=os.getenv('PORTAL_DB_HOST', portal.get('host', 'postgres')),
            port=int(os.getenv('PORTAL_DB_PORT', portal.get('port', 5432))),
            database=os.getenv('PORTAL_DB_NAME', portal.get('database', 'greenway')),
            user=os.getenv('PORTAL_DB_USER', portal.get('user', 'greenway')),
            password=os.getenv('PORTAL_DB_PASSWORD', portal.get('password', 'secret')),
        )

    def get_merge_strategy(self) -> dict:
        """Get field merge priority configuration."""
        return self.config.get('merge_strategy', {})

    def get_completeness_weights(self) -> dict:
        """Get completeness scoring weights."""
        return self.config.get('completeness_weights', {})

    def get_mandatory_fields(self) -> list[str]:
        """Get list of mandatory fields for promotion."""
        return self.config.get('mandatory_fields', [])

    def get_enrichment_layers(self) -> dict:
        """Get enrichment layer configuration."""
        return self.config.get('enrichment_layers', {})

    def get_promotion_rules(self) -> dict:
        """Get promotion rules configuration."""
        return self.config.get('promotion', {})


# Singleton instance
_config_instance = None

def get_enrichment_config() -> EnrichmentConfig:
    """Get singleton enrichment configuration instance."""
    global _config_instance
    if _config_instance is None:
        _config_instance = EnrichmentConfig()
    return _config_instance
