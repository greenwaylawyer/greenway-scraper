"""Data export utilities.

Exports scraped data to various formats:
- JSON (for testing/validation)
- PostgreSQL staging table (for production)
"""

import json
import os
import uuid
from datetime import datetime
from typing import Optional
from pathlib import Path
from scrapers.base import LawyerRawData
from normalizers.address import AddressNormalizer
from normalizers.phone import PhoneNormalizer
from normalizers.practice_areas import PracticeAreaNormalizer
from normalizers.name import NameNormalizer
from pipeline.dedup import FingerprintGenerator
from pipeline.scorer import CompletenessScorer
from utils.logger import get_logger

logger = get_logger(__name__)


class DataExporter:
    """Export scraped data to various formats."""

    def __init__(self, state: str, source: str, batch_id: Optional[str] = None):
        """
        Initialize exporter.

        Args:
            state: 2-letter state code
            source: Source identifier (e.g., 'calbar', 'nybar')
            batch_id: Optional existing batch ID (for reusing batch tracker)
        """
        self.state = state
        self.source = source
        self.batch_id = uuid.UUID(batch_id) if batch_id else uuid.uuid4()

        # Ensure output directory exists
        self.output_dir = Path("output")
        self.output_dir.mkdir(exist_ok=True)

        logger.info(
            "Exporter initialized",
            state=state,
            source=source,
            batch_id=str(self.batch_id),
        )

    def export_json(self, lawyers: list[LawyerRawData], filename: Optional[str] = None) -> Path:
        """
        Export lawyer data to JSON file.

        Args:
            lawyers: List of LawyerRawData objects
            filename: Optional filename (default: auto-generated)

        Returns:
            Path to exported file
        """
        if not filename:
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{self.state}_{self.source}_{timestamp}.json"

        output_path = self.output_dir / filename

        # Convert to normalized format
        normalized = [self._normalize_lawyer(lawyer) for lawyer in lawyers]

        # Write to file
        with open(output_path, 'w') as f:
            json.dump({
                'batch_id': str(self.batch_id),
                'state': self.state,
                'source': self.source,
                'timestamp': datetime.now().isoformat(),
                'count': len(normalized),
                'lawyers': normalized,
            }, f, indent=2)

        logger.info(
            "Exported to JSON",
            path=str(output_path),
            count=len(normalized),
        )

        return output_path

    def normalize_for_db(self, lawyers: list[LawyerRawData]) -> list[dict]:
        """
        Normalize lawyer data for database import.

        Returns data in the format expected by lawyer_imports table.

        Args:
            lawyers: List of LawyerRawData objects

        Returns:
            List of normalized dictionaries ready for DB import
        """
        normalized = []

        for lawyer in lawyers:
            record = self._normalize_lawyer(lawyer)
            normalized.append(record)

        return normalized

    def export_to_database(self, lawyers: list[LawyerRawData], layer_name: str = "state_bars", enrichment_level: int = 1) -> int:
        """
        Export lawyer data to PostgreSQL scraper database (lawyer_enrichment table).
        Supports multi-layer enrichment tracking.

        Args:
            lawyers: List of LawyerRawData objects
            layer_name: Enrichment layer (e.g., 'state_bars', 'justia', 'courtlistener')
            enrichment_level: Enrichment level (1=list, 2=detail, 3=multi-source, etc.)

        Returns:
            Number of records inserted/updated

        Raises:
            Exception: If database connection fails or import errors
        """
        try:
            import psycopg2
            from psycopg2.extras import Json
            from dotenv import load_dotenv
        except ImportError as e:
            logger.error("Required module not installed", error=str(e))
            raise Exception("psycopg2-binary and python-dotenv required. Install with: pip install psycopg2-binary python-dotenv")

        # Load environment variables from .env file
        # Use explicit path to ensure .env is found regardless of cwd
        env_path = Path(__file__).parent.parent / '.env'
        logger.info(f"Loading .env from: {env_path}, file exists: {env_path.exists()}")

        # override=True ensures .env values take precedence over existing env vars
        load_dotenv(env_path, override=True)

        logger.info(f"After load_dotenv - SCRAPER_DB_HOST={os.getenv('SCRAPER_DB_HOST')}, SCRAPER_DB_PORT={os.getenv('SCRAPER_DB_PORT')}")

        # Get database connection details for SCRAPER database
        db_mode = os.getenv('DATABASE_MODE', 'scraper')

        if db_mode == 'scraper':
            db_host = os.getenv('SCRAPER_DB_HOST', '127.0.0.1')
            db_port = int(os.getenv('SCRAPER_DB_PORT', '5433'))
            db_name = os.getenv('SCRAPER_DB_NAME', 'greenway_scraper')
            db_user = os.getenv('SCRAPER_DB_USER', 'scraper')
            db_password = os.getenv('SCRAPER_DB_PASSWORD', 'scraper123')
        else:
            # Legacy mode: write to portal database
            db_host = os.getenv('PORTAL_DB_HOST', 'postgres')
            db_port = int(os.getenv('PORTAL_DB_PORT', '5432'))
            db_name = os.getenv('PORTAL_DB_NAME', 'greenway')
            db_user = os.getenv('PORTAL_DB_USER', 'greenway')
            db_password = os.getenv('PORTAL_DB_PASSWORD', 'secret')

        logger.info(
            "Connecting to database",
            mode=db_mode,
            host=db_host,
            port=db_port,
            database=db_name,
            layer=layer_name,
        )

        # Connect to database
        try:
            conn = psycopg2.connect(
                host=db_host,
                port=db_port,
                dbname=db_name,
                user=db_user,
                password=db_password,
            )
            cursor = conn.cursor()
        except Exception as e:
            logger.error("Database connection failed", error=str(e))
            raise

        # Create or update enrichment batch record
        # Check if batch already exists (created by BatchTracker)
        cursor.execute("SELECT batch_id FROM enrichment_batches WHERE batch_id = %s", (str(self.batch_id),))
        batch_exists = cursor.fetchone() is not None

        if batch_exists:
            # Update existing batch with total records
            update_batch_query = """
                UPDATE enrichment_batches
                SET total_records = %s,
                    updated_at = NOW()
                WHERE batch_id = %s
            """
            cursor.execute(update_batch_query, (len(lawyers), str(self.batch_id)))
            logger.info("Updated existing batch record", batch_id=str(self.batch_id))
        else:
            # Create new batch record (backwards compatibility)
            batch_query = """
                INSERT INTO enrichment_batches (
                    batch_id, layer_name, state, total_records, status
                ) VALUES (%s, %s, %s, %s, %s)
            """
            cursor.execute(batch_query, (
                str(self.batch_id),
                layer_name,
                self.state.upper(),
                len(lawyers),
                'running'
            ))
            logger.info("Created new batch record", batch_id=str(self.batch_id))

        conn.commit()

        # Normalize lawyer data
        normalized = self.normalize_for_db(lawyers)

        # Prepare UPSERT statement for lawyer_enrichment table
        # Build dynamic level completion field name
        level_completed_field = f"level_{enrichment_level}_completed_at"

        # Helper macro: wrap a scalar field update with manual-curation protection.
        # When manually_curated=true AND the field name is in manually_curated_fields
        # AND the incoming enrichment level is < 4 (i.e. not a reviews/ratings layer),
        # the existing manually-curated value is preserved unchanged.
        # For layer >= 4 (reviews, ratings, advanced) the guard is bypassed entirely
        # so those fields are always refreshed from the scraper.
        def _guarded(field: str, higher_level_expr: str, lower_level_expr: str) -> str:
            """
            Returns a CASE expression that:
              1. Preserves the DB value when the field is manually curated (layers 1-3).
              2. Otherwise applies the standard level-precedence logic.
            """
            return f"""CASE
                    WHEN lawyer_enrichment.manually_curated
                         AND (lawyer_enrichment.manually_curated_fields ? '{field}')
                         AND %(enrichment_level)s < 4
                    THEN lawyer_enrichment.{field}
                    WHEN EXCLUDED.enrichment_level >= lawyer_enrichment.enrichment_level
                    THEN {higher_level_expr}
                    ELSE {lower_level_expr}
                END"""

        def _guarded_simple(field: str, coalesce_expr: str) -> str:
            """For fields that don't use level-precedence (COALESCE only)."""
            return f"""CASE
                    WHEN lawyer_enrichment.manually_curated
                         AND (lawyer_enrichment.manually_curated_fields ? '{field}')
                         AND %(enrichment_level)s < 4
                    THEN lawyer_enrichment.{field}
                    ELSE {coalesce_expr}
                END"""

        upsert_query = f"""
            INSERT INTO lawyer_enrichment (
                fingerprint,
                enrichment_layers,
                enrichment_level,
                {level_completed_field},
                raw_data_by_source,
                merged_data,
                completeness_score,
                full_name, first_name, last_name, bar_number,
                license_state, license_status, admission_date, firm_name,
                city, state
            ) VALUES (
                %(fingerprint)s,
                %(enrichment_layers)s,
                %(enrichment_level)s,
                NOW(),
                %(raw_data_by_source)s,
                %(merged_data)s,
                %(completeness_score)s,
                %(full_name)s, %(first_name)s, %(last_name)s, %(bar_number)s,
                %(license_state)s, %(license_status)s, %(admission_date)s, %(firm_name)s,
                %(city)s, %(state)s
            )
            ON CONFLICT (fingerprint) DO UPDATE SET
                -- ── Pipeline / meta fields — NEVER protected by manual curation ──────────
                enrichment_layers = lawyer_enrichment.enrichment_layers || EXCLUDED.enrichment_layers,
                enrichment_level = GREATEST(lawyer_enrichment.enrichment_level, EXCLUDED.enrichment_level),
                {level_completed_field} = NOW(),
                raw_data_by_source = lawyer_enrichment.raw_data_by_source || EXCLUDED.raw_data_by_source,
                -- Only overwrite merged_data when incoming level is >= stored level
                merged_data = CASE
                    WHEN EXCLUDED.enrichment_level >= lawyer_enrichment.enrichment_level
                    THEN EXCLUDED.merged_data
                    ELSE lawyer_enrichment.merged_data
                END,
                -- Completeness score: always take the better score
                completeness_score = GREATEST(EXCLUDED.completeness_score, lawyer_enrichment.completeness_score),

                -- ── Scalar fields: protected when manually curated (layer < 4) ─────────
                -- Name fields
                full_name = {_guarded(
                    'full_name',
                    'COALESCE(EXCLUDED.full_name, lawyer_enrichment.full_name)',
                    'COALESCE(lawyer_enrichment.full_name, EXCLUDED.full_name)',
                )},
                first_name = {_guarded(
                    'first_name',
                    'COALESCE(EXCLUDED.first_name, lawyer_enrichment.first_name)',
                    'COALESCE(lawyer_enrichment.first_name, EXCLUDED.first_name)',
                )},
                last_name = {_guarded(
                    'last_name',
                    'COALESCE(EXCLUDED.last_name, lawyer_enrichment.last_name)',
                    'COALESCE(lawyer_enrichment.last_name, EXCLUDED.last_name)',
                )},

                -- Identity (bar_number / license_state never overwritten — always keep first value)
                bar_number = COALESCE(lawyer_enrichment.bar_number, EXCLUDED.bar_number),
                license_state = COALESCE(lawyer_enrichment.license_state, EXCLUDED.license_state),

                -- License status: freshest value wins, but curated overrides
                license_status = {_guarded_simple(
                    'license_status',
                    'COALESCE(EXCLUDED.license_status, lawyer_enrichment.license_status)',
                )},

                admission_date = {_guarded_simple(
                    'admission_date',
                    'COALESCE(lawyer_enrichment.admission_date, EXCLUDED.admission_date)',
                )},

                firm_name = {_guarded(
                    'firm_name',
                    'COALESCE(EXCLUDED.firm_name, lawyer_enrichment.firm_name)',
                    'COALESCE(lawyer_enrichment.firm_name, EXCLUDED.firm_name)',
                )},
                city = {_guarded(
                    'city',
                    'COALESCE(EXCLUDED.city, lawyer_enrichment.city)',
                    'COALESCE(lawyer_enrichment.city, EXCLUDED.city)',
                )},
                state = {_guarded_simple(
                    'state',
                    'COALESCE(lawyer_enrichment.state, EXCLUDED.state)',
                )},

                -- ── Always-updated pipeline timestamps ────────────────────────────────────
                last_enriched_at = NOW(),
                updated_at = NOW()
        """

        # Insert/update records
        inserted = 0
        updated = 0
        errors = 0

        for record in normalized:
            try:
                # Prepare enrichment layer metadata
                enrichment_layers = Json({
                    layer_name: {
                        'scraped_at': datetime.now().isoformat(),
                        'batch_id': str(self.batch_id),
                        'source': self.source,
                    }
                })

                # Build raw data by source
                raw_data_by_source = Json({
                    layer_name: record.get('raw_data', {})
                })

                # Merged data includes ALL fields (even if not in normalized columns)
                merged_data = Json({
                    'full_name': record.get('full_name'),
                    'first_name': record.get('first_name'),
                    'last_name': record.get('last_name'),
                    'bar_number': record.get('bar_number'),
                    'firm_name': record.get('firm_name'),
                    'address': {
                        'line1': record.get('address_line1'),
                        'line2': record.get('address_line2'),
                        'city': record.get('city'),
                        'state': record.get('state'),
                        'zip': record.get('zip'),
                    },
                    'phone': record.get('phone'),
                    'fax': record.get('fax'),
                    'email': record.get('email'),
                    'website_url': record.get('website_url'),
                    'practice_areas': record.get('practice_areas', []),
                    'bio': record.get('bio'),
                    'photo_url': record.get('photo_url'),
                    'law_school': record.get('law_school'),
                    'law_school_grad_year': record.get('law_school_grad_year'),
                    'license_state': record.get('license_state'),
                    'license_status': record.get('license_status'),
                    'admission_date': record.get('admission_date'),
                    'detail_url': record.get('detail_url'),
                })

                # Prepare insert data with only the columns that exist in the schema
                record_enriched = {
                    'fingerprint': record.get('fingerprint'),
                    'enrichment_layers': enrichment_layers,
                    'enrichment_level': enrichment_level,
                    'raw_data_by_source': raw_data_by_source,
                    'merged_data': merged_data,
                    'completeness_score': record.get('completeness_score'),
                    'full_name': record.get('full_name'),
                    'first_name': record.get('first_name'),
                    'last_name': record.get('last_name'),
                    'bar_number': record.get('bar_number'),
                    'license_state': record.get('license_state'),
                    'license_status': record.get('license_status'),
                    'admission_date': record.get('admission_date'),
                    'firm_name': record.get('firm_name'),
                    'city': record.get('city'),
                    'state': record.get('state'),
                }

                cursor.execute(upsert_query, record_enriched)

                # Check if this was an insert or update
                if cursor.rowcount > 0:
                    inserted += 1
                else:
                    updated += 1

                if (inserted + updated) % 100 == 0:
                    conn.commit()
                    logger.info("Batch committed", inserted=inserted, updated=updated)

            except Exception as e:
                errors += 1
                logger.error(
                    "Failed to insert/update record",
                    error=str(e),
                    record=record.get('full_name', 'Unknown'),
                )

                # Log to enrichment_errors table
                try:
                    error_query = """
                        INSERT INTO enrichment_errors (
                            layer_name, state, lawyer_fingerprint, error_message, error_context
                        ) VALUES (%s, %s, %s, %s, %s)
                    """
                    cursor.execute(error_query, (
                        layer_name,
                        self.state.upper(),
                        record.get('fingerprint'),
                        str(e),
                        Json({'full_name': record.get('full_name'), 'bar_number': record.get('bar_number')})
                    ))
                    conn.commit()
                except:
                    pass  # Don't fail if error logging fails

                continue

        # Final commit
        conn.commit()

        # Update batch status
        update_batch_query = """
            UPDATE enrichment_batches
            SET status = %s,
                records_processed = %s,
                records_failed = %s,
                completed_at = NOW()
            WHERE batch_id = %s
        """
        cursor.execute(update_batch_query, (
            'completed',
            inserted + updated,
            errors,
            str(self.batch_id)
        ))
        conn.commit()

        cursor.close()
        conn.close()

        logger.info(
            "Database export complete",
            inserted=inserted,
            updated=updated,
            errors=errors,
            batch_id=str(self.batch_id),
            layer=layer_name,
        )

        return inserted + updated

    def _normalize_lawyer(self, lawyer: LawyerRawData) -> dict:
        """Normalize a single lawyer record."""

        # Parse name
        name_data = NameNormalizer.normalize(lawyer.full_name)

        # Parse address
        address_data = AddressNormalizer.normalize(lawyer.address)

        # Normalize phone
        phone_normalized = PhoneNormalizer.normalize(lawyer.phone)

        # Normalize practice areas — use AI normalizer if enabled, else fall back to rules
        try:
            from ai import config as ai_config
            if ai_config.is_feature_enabled("practice_area_normalization"):
                from ai.normalizers.practice_areas_ai import get_normalizer as get_ai_pa
                practice_areas_normalized = get_ai_pa().normalize(lawyer.practice_areas)
            else:
                practice_areas_normalized = PracticeAreaNormalizer.normalize(lawyer.practice_areas)
        except Exception:
            practice_areas_normalized = PracticeAreaNormalizer.normalize(lawyer.practice_areas)

        # Generate fingerprint
        fingerprint = FingerprintGenerator.generate(lawyer, self.state)

        # Calculate completeness score
        score = CompletenessScorer.score(lawyer)

        # Build record
        record = {
            # Source info
            'source': self.source,
            'source_id': lawyer.bar_number,
            'raw_data': {
                'full_name': lawyer.full_name,
                'middle_name': lawyer.middle_name,
                'bar_number': lawyer.bar_number,
                'firm_name': lawyer.firm_name,
                'address': lawyer.address,
                'phone': lawyer.phone,
                'fax': lawyer.fax,
                'email': lawyer.email,
                'website_url': lawyer.website_url,
                'practice_areas': lawyer.practice_areas,
                'bio': lawyer.bio,
                'photo_url': lawyer.photo_url,
                'law_school': lawyer.law_school,
                'admission_year': lawyer.admission_year,
                'license_status': lawyer.license_status,
                'detail_url': lawyer.detail_url,
                'raw_html': lawyer.raw_html,
            },

            # Batch ID
            'import_batch_id': str(self.batch_id),

            # Normalized fields
            'full_name': name_data.get('full_name'),
            'first_name': name_data.get('first_name'),
            'last_name': name_data.get('last_name'),
            'middle_name': (
                lawyer.middle_name                      # use field value if scraper provided it
                or name_data.get('middle_name')         # fall back to what NameNormalizer parsed
            ),
            'bar_number': lawyer.bar_number,
            'license_state': self.state.upper(),
            'license_status': lawyer.license_status,
            'admission_date': f"{lawyer.admission_year}-01-01" if lawyer.admission_year else None,
            'firm_name': lawyer.firm_name,
            'address_line1': address_data.get('address_line1') if address_data else None,
            'address_line2': address_data.get('address_line2') if address_data else None,
            'city': address_data.get('city') if address_data else None,
            'state': address_data.get('state') if address_data else None,
            'zip': address_data.get('zip') if address_data else None,
            'phone': phone_normalized,
            'fax': PhoneNormalizer.normalize(lawyer.fax),
            'email': lawyer.email,
            'website_url': lawyer.website_url,
            'detail_url': lawyer.detail_url,
            'practice_areas': list(practice_areas_normalized) if practice_areas_normalized else None,
            'bio': lawyer.bio,
            'photo_url': lawyer.photo_url,
            'law_school': lawyer.law_school,
            'law_school_grad_year': lawyer.admission_year,

            # Pipeline state
            'fingerprint': fingerprint,
            'completeness_score': score,
            'status': 'pending',  # Will be processed by Laravel jobs
        }

        return record
