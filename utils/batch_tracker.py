"""Batch tracking for real-time status updates during scraping.

This module provides real-time batch status updates to the database
so the admin panel can display live scraping progress.
"""

import os
import uuid
from datetime import datetime
from typing import Optional
from pathlib import Path
from dotenv import load_dotenv
from utils.logger import get_logger

logger = get_logger(__name__)


class BatchTracker:
    """Tracks batch progress and updates database in real-time."""

    def __init__(self, state: str, layer_name: str, batch_id: Optional[str] = None):
        """
        Initialize batch tracker.

        Args:
            state: 2-letter state code (e.g., 'CA')
            layer_name: Enrichment layer (e.g., 'state_bars')
            batch_id: Optional existing batch ID, or generates new UUID
        """
        self.state = state.upper()
        self.layer_name = layer_name
        self.batch_id = batch_id or str(uuid.uuid4())
        self.conn = None
        self.cursor = None

        # Load environment variables
        env_path = Path(__file__).parent.parent / '.env'
        load_dotenv(env_path, override=True)

        logger.info(
            "BatchTracker initialized",
            batch_id=self.batch_id,
            state=self.state,
            layer=layer_name,
        )

    def _connect(self):
        """Establish database connection if not already connected."""
        if self.conn is not None:
            return

        try:
            import psycopg2

            db_mode = os.getenv('DATABASE_MODE', 'scraper')

            if db_mode == 'scraper':
                db_host = os.getenv('SCRAPER_DB_HOST', '127.0.0.1')
                db_port = int(os.getenv('SCRAPER_DB_PORT', '5433'))
                db_name = os.getenv('SCRAPER_DB_NAME', 'greenway_scraper')
                db_user = os.getenv('SCRAPER_DB_USER', 'scraper')
                db_password = os.getenv('SCRAPER_DB_PASSWORD', 'scraper123')
            else:
                db_host = os.getenv('PORTAL_DB_HOST', 'postgres')
                db_port = int(os.getenv('PORTAL_DB_PORT', '5432'))
                db_name = os.getenv('PORTAL_DB_NAME', 'greenway')
                db_user = os.getenv('PORTAL_DB_USER', 'greenway')
                db_password = os.getenv('PORTAL_DB_PASSWORD', 'secret')

            self.conn = psycopg2.connect(
                host=db_host,
                port=db_port,
                dbname=db_name,
                user=db_user,
                password=db_password,
            )
            self.cursor = self.conn.cursor()

            logger.info("Database connected for batch tracking")

        except Exception as e:
            logger.error("Failed to connect to database for batch tracking", error=str(e))
            self.conn = None
            self.cursor = None

    def create_batch(self, total_records: int = 0, enrichment_level: int = 1, metadata: Optional[dict] = None):
        """
        Create initial batch record with 'running' status.

        Args:
            total_records: Estimated total records (can be updated later)
            enrichment_level: Enrichment level (1-5)
            metadata: Optional metadata dictionary
        """
        self._connect()

        if not self.conn:
            logger.warning("Cannot create batch - no database connection")
            return

        try:
            from psycopg2.extras import Json

            query = """
                INSERT INTO enrichment_batches (
                    batch_id, layer_name, state, total_records, enrichment_level, status, metadata
                ) VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (batch_id) DO UPDATE SET
                    total_records = EXCLUDED.total_records,
                    enrichment_level = EXCLUDED.enrichment_level,
                    metadata = EXCLUDED.metadata,
                    updated_at = NOW()
            """
            self.cursor.execute(query, (
                self.batch_id,
                self.layer_name,
                self.state,
                total_records,
                enrichment_level,
                'running',
                Json(metadata or {})
            ))
            self.conn.commit()

            logger.info(
                "Batch created in database",
                batch_id=self.batch_id,
                level=enrichment_level,
                status='running',
            )

        except Exception as e:
            logger.error("Failed to create batch", error=str(e))

    def update_progress(
        self,
        records_processed: int = None,
        records_created: int = None,
        records_updated: int = None,
        records_failed: int = None,
        progress_percent: float = None,
        metadata: Optional[dict] = None,
    ):
        """
        Update batch progress.

        Args:
            records_processed: Total records processed so far
            records_created: New records created
            records_updated: Existing records updated
            records_failed: Records that failed
            progress_percent: Progress percentage (0-100)
            metadata: Optional metadata to merge
        """
        self._connect()

        if not self.conn:
            return

        try:
            from psycopg2.extras import Json

            # Build dynamic update query
            updates = ["updated_at = NOW()"]
            params = []

            if records_processed is not None:
                updates.append("records_processed = %s")
                params.append(records_processed)

            if records_created is not None:
                updates.append("records_created = %s")
                params.append(records_created)

            if records_updated is not None:
                updates.append("records_updated = %s")
                params.append(records_updated)

            if records_failed is not None:
                updates.append("records_failed = %s")
                params.append(records_failed)

            if progress_percent is not None:
                updates.append("progress_percent = %s")
                params.append(progress_percent)

            if metadata is not None:
                updates.append("metadata = metadata || %s")
                params.append(Json(metadata))

            params.append(self.batch_id)

            query = f"""
                UPDATE enrichment_batches
                SET {', '.join(updates)}
                WHERE batch_id = %s
            """

            self.cursor.execute(query, params)
            self.conn.commit()

            logger.debug(
                "Batch progress updated",
                batch_id=self.batch_id,
                records_processed=records_processed,
            )

        except Exception as e:
            logger.error("Failed to update batch progress", error=str(e))

    def log_error(
        self,
        error_message: str,
        error_type: Optional[str] = None,
        error_stacktrace: Optional[str] = None,
        context_data: Optional[dict] = None,
    ):
        """
        Insert a row into enrichment_errors for this batch.

        Args:
            error_message: The error message text
            error_type: Short error category (e.g. 'TimeoutError', 'ScrapingFailed')
            error_stacktrace: Full traceback string, if available
            context_data: Optional dict of extra context to store as JSONB
        """
        self._connect()

        if not self.conn:
            logger.warning("Cannot log error - no database connection")
            return

        try:
            from psycopg2.extras import Json

            query = """
                INSERT INTO enrichment_errors (
                    batch_id, error_type, error_message, error_stacktrace,
                    layer_name, context_data
                ) VALUES (%s, %s, %s, %s, %s, %s)
            """
            self.cursor.execute(query, (
                self.batch_id,
                error_type,
                error_message,
                error_stacktrace,
                self.layer_name,
                Json(context_data or {}),
            ))
            self.conn.commit()

            logger.info(
                "Error logged to enrichment_errors",
                batch_id=self.batch_id,
                error_type=error_type,
            )

        except Exception as e:
            logger.error("Failed to log error to enrichment_errors", error=str(e))

    def complete_batch(self, status: str = 'completed', error_log: Optional[str] = None):
        """
        Mark batch as completed or failed.

        Args:
            status: Final status ('completed', 'failed', 'cancelled')
            error_log: Optional error message if failed
        """
        self._connect()

        if not self.conn:
            return

        try:
            query = """
                UPDATE enrichment_batches
                SET status = %s,
                    completed_at = NOW(),
                    duration_seconds = EXTRACT(EPOCH FROM (NOW() - started_at))::INTEGER,
                    error_log = COALESCE(%s, error_log),
                    updated_at = NOW()
                WHERE batch_id = %s
            """

            self.cursor.execute(query, (status, error_log, self.batch_id))
            self.conn.commit()

            logger.info(
                "Batch completed",
                batch_id=self.batch_id,
                status=status,
            )

        except Exception as e:
            logger.error("Failed to complete batch", error=str(e))

    def close(self):
        """Close database connection."""
        if self.cursor:
            self.cursor.close()
        if self.conn:
            self.conn.close()

        logger.debug("BatchTracker closed")

    def __enter__(self):
        """Context manager entry."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit."""
        if exc_type is not None:
            # Exception occurred - log to enrichment_errors and mark as failed
            error_msg = f"{exc_type.__name__}: {exc_val}"
            self.log_error(
                error_message=error_msg,
                error_type=exc_type.__name__,
            )
            self.complete_batch(status='failed', error_log=error_msg)

        self.close()
