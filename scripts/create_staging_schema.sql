-- ============================================================================
-- Greenway Scraper - Staging Database Schema
-- ============================================================================
-- This schema supports the multi-layer enrichment pipeline.
-- Profiles accumulate here and get enriched until ready for promotion.
-- ============================================================================

-- Drop tables if they exist (for clean reinstall)
DROP TABLE IF EXISTS enrichment_progress CASCADE;
DROP TABLE IF EXISTS lawyer_staging CASCADE;

-- ============================================================================
-- Table: lawyer_staging
-- ============================================================================
-- Stores lawyer profiles as they get enriched through multiple data sources.
-- Tracks completeness and promotion readiness.

CREATE TABLE lawyer_staging (
    id BIGSERIAL PRIMARY KEY,

    -- ========================================================================
    -- Deduplication & Identity
    -- ========================================================================
    fingerprint VARCHAR(64) UNIQUE NOT NULL,
    -- Generated from: normalize(name) + bar_number + license_state
    -- Used to identify the same lawyer across different sources

    -- ========================================================================
    -- Enrichment Tracking
    -- ========================================================================
    enrichment_layers JSONB DEFAULT '[]'::jsonb,
    -- Example: ["state_bars", "justia", "google_places"]
    -- Tracks which enrichment layers have been applied

    completeness_score SMALLINT DEFAULT 0 CHECK (completeness_score >= 0 AND completeness_score <= 100),
    -- Score from 0-100 based on configured weights
    -- Calculated after each enrichment layer

    last_enriched_at TIMESTAMP,
    -- Timestamp of last enrichment update

    ready_for_promotion BOOLEAN DEFAULT false,
    -- True when completeness >= threshold and all checks pass

    -- ========================================================================
    -- Raw Data Storage (by source)
    -- ========================================================================
    raw_data_by_source JSONB DEFAULT '{}'::jsonb,
    -- Preserves raw data from each source for auditing
    -- Example structure:
    -- {
    --   "calbar": {
    --     "full_name": "John A. Doe",
    --     "bar_number": "123456",
    --     "license_status": "Active",
    --     "scraped_at": "2026-03-21T10:00:00Z"
    --   },
    --   "justia": {
    --     "bio": "Experienced attorney...",
    --     "practice_areas": ["Family Law", "Divorce"],
    --     "photo_url": "https://...",
    --     "scraped_at": "2026-03-21T11:00:00Z"
    --   }
    -- }

    -- ========================================================================
    -- Merged Data (best from all sources)
    -- ========================================================================
    merged_data JSONB NOT NULL,
    -- Contains the "best" data merged from all sources using priority rules
    -- This is what gets promoted to production
    -- Includes field_source tracking: { "phone": "+1234567890", "phone_source": "google_places" }

    -- ========================================================================
    -- Normalized Fields (for fast querying)
    -- ========================================================================
    -- These are denormalized from merged_data for indexing/filtering

    full_name VARCHAR(200),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    middle_name VARCHAR(100),

    bar_number VARCHAR(50),
    license_state CHAR(2),
    license_status VARCHAR(30),
    admission_date DATE,

    firm_name VARCHAR(200),

    address_line1 VARCHAR(200),
    address_line2 VARCHAR(200),
    city VARCHAR(100),
    state CHAR(2),
    zip VARCHAR(10),

    phone VARCHAR(20),
    fax VARCHAR(20),
    email VARCHAR(200),
    website_url VARCHAR(500),

    -- ========================================================================
    -- Promotion Control
    -- ========================================================================
    promotion_blocked BOOLEAN DEFAULT false,
    promotion_blocked_reason TEXT,
    -- Set to true if profile has issues preventing promotion
    -- Examples: duplicate detected, conflicting data, missing mandatory field

    manual_review_required BOOLEAN DEFAULT false,
    -- Flag for edge cases that need human review

    promoted_at TIMESTAMP,
    promoted_to_lawyer_id UUID,
    -- Track if/when this was promoted to production

    -- ========================================================================
    -- Metadata
    -- ========================================================================
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW(),
    deleted_at TIMESTAMP
    -- Soft delete support
);

-- ============================================================================
-- Indexes for lawyer_staging
-- ============================================================================

CREATE INDEX idx_staging_fingerprint ON lawyer_staging(fingerprint);
CREATE INDEX idx_staging_completeness ON lawyer_staging(completeness_score DESC);
CREATE INDEX idx_staging_ready ON lawyer_staging(ready_for_promotion) WHERE ready_for_promotion = true;
CREATE INDEX idx_staging_state ON lawyer_staging(license_state);
CREATE INDEX idx_staging_bar_number ON lawyer_staging(bar_number);
CREATE INDEX idx_staging_layers ON lawyer_staging USING gin(enrichment_layers);
CREATE INDEX idx_staging_raw_data ON lawyer_staging USING gin(raw_data_by_source);
CREATE INDEX idx_staging_merged_data ON lawyer_staging USING gin(merged_data);
CREATE INDEX idx_staging_name ON lawyer_staging(last_name, first_name);
CREATE INDEX idx_staging_created ON lawyer_staging(created_at DESC);
CREATE INDEX idx_staging_updated ON lawyer_staging(updated_at DESC);
CREATE INDEX idx_staging_manual_review ON lawyer_staging(manual_review_required) WHERE manual_review_required = true;
CREATE INDEX idx_staging_promoted ON lawyer_staging(promoted_at) WHERE promoted_at IS NOT NULL;

-- ============================================================================
-- Table: enrichment_progress
-- ============================================================================
-- Tracks progress of enrichment jobs for monitoring/UI

CREATE TABLE enrichment_progress (
    id BIGSERIAL PRIMARY KEY,

    -- ========================================================================
    -- Job Identity
    -- ========================================================================
    layer_name VARCHAR(50) NOT NULL,
    -- One of: state_bars, justia, courtlistener, google_places

    batch_id UUID NOT NULL,
    -- Unique identifier for this enrichment run

    state_code CHAR(2),
    -- Optional: if scraping a specific state

    -- ========================================================================
    -- Timing
    -- ========================================================================
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMP,

    status VARCHAR(20) DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'paused')),

    -- ========================================================================
    -- Metrics
    -- ========================================================================
    records_processed INT DEFAULT 0,
    -- Total records processed so far

    records_created INT DEFAULT 0,
    -- New records created in staging

    records_updated INT DEFAULT 0,
    -- Existing records updated/enriched

    records_skipped INT DEFAULT 0,
    -- Records skipped (already enriched, etc.)

    errors INT DEFAULT 0,
    -- Number of errors encountered

    -- ========================================================================
    -- Progress Tracking (for UI)
    -- ========================================================================
    total_records INT,
    -- Total expected records (if known)

    current_record INT DEFAULT 0,
    -- Current position in the batch

    progress_percent DECIMAL(5,2) DEFAULT 0 CHECK (progress_percent >= 0 AND progress_percent <= 100),
    -- Percentage complete

    estimated_completion_at TIMESTAMP,
    -- Estimated time to complete (based on current rate)

    -- ========================================================================
    -- Quality Metrics
    -- ========================================================================
    avg_completeness_before DECIMAL(5,2),
    avg_completeness_after DECIMAL(5,2),
    -- Average completeness scores before/after this layer

    records_promoted INT DEFAULT 0,
    -- How many records became ready for promotion after this layer

    -- ========================================================================
    -- Error Logging
    -- ========================================================================
    error_log TEXT,
    -- JSON or text log of errors encountered

    last_checkpoint JSONB,
    -- Checkpoint data for resuming interrupted jobs
    -- Example: { "last_page": 42, "last_record_id": 12345 }

    -- ========================================================================
    -- Configuration Snapshot
    -- ========================================================================
    config_snapshot JSONB,
    -- Snapshot of enrichment config used for this run
    -- Useful for auditing/debugging

    -- ========================================================================
    -- Metadata
    -- ========================================================================
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP DEFAULT NOW()
);

-- ============================================================================
-- Indexes for enrichment_progress
-- ============================================================================

CREATE INDEX idx_progress_layer ON enrichment_progress(layer_name, status);
CREATE INDEX idx_progress_batch ON enrichment_progress(batch_id);
CREATE INDEX idx_progress_status ON enrichment_progress(status) WHERE status IN ('running', 'paused');
CREATE INDEX idx_progress_started ON enrichment_progress(started_at DESC);
CREATE INDEX idx_progress_state ON enrichment_progress(state_code) WHERE state_code IS NOT NULL;

-- ============================================================================
-- Views (for convenience)
-- ============================================================================

-- View: Current enrichment stats by completeness bucket
CREATE OR REPLACE VIEW enrichment_stats AS
SELECT
    CASE
        WHEN completeness_score < 40 THEN '0-39%'
        WHEN completeness_score < 60 THEN '40-59%'
        WHEN completeness_score < 80 THEN '60-79%'
        ELSE '80-100%'
    END as completeness_bucket,
    COUNT(*) as record_count,
    AVG(completeness_score) as avg_completeness,
    SUM(CASE WHEN ready_for_promotion THEN 1 ELSE 0 END) as ready_for_promotion_count
FROM lawyer_staging
WHERE deleted_at IS NULL
GROUP BY completeness_bucket
ORDER BY MIN(completeness_score);

-- View: Enrichment layer stats
CREATE OR REPLACE VIEW layer_coverage AS
SELECT
    layer.value::text as layer_name,
    COUNT(*) as lawyer_count,
    AVG(completeness_score) as avg_completeness
FROM lawyer_staging,
     jsonb_array_elements(enrichment_layers) as layer
WHERE deleted_at IS NULL
GROUP BY layer_name
ORDER BY lawyer_count DESC;

-- View: Recent enrichment jobs
CREATE OR REPLACE VIEW recent_enrichment_jobs AS
SELECT
    layer_name,
    batch_id,
    started_at,
    completed_at,
    status,
    records_processed,
    records_updated,
    records_created,
    errors,
    progress_percent,
    EXTRACT(EPOCH FROM (COALESCE(completed_at, NOW()) - started_at)) as duration_seconds
FROM enrichment_progress
ORDER BY started_at DESC
LIMIT 100;

-- ============================================================================
-- Functions
-- ============================================================================

-- Function: Update updated_at timestamp automatically
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Trigger: Auto-update updated_at for lawyer_staging
CREATE TRIGGER update_lawyer_staging_updated_at
    BEFORE UPDATE ON lawyer_staging
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- Trigger: Auto-update updated_at for enrichment_progress
CREATE TRIGGER update_enrichment_progress_updated_at
    BEFORE UPDATE ON enrichment_progress
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- Sample Queries (commented out - for reference)
-- ============================================================================

-- Find lawyers ready for promotion:
-- SELECT id, full_name, bar_number, completeness_score, enrichment_layers
-- FROM lawyer_staging
-- WHERE ready_for_promotion = true
-- ORDER BY completeness_score DESC;

-- Check enrichment progress:
-- SELECT * FROM enrichment_stats;
-- SELECT * FROM layer_coverage;
-- SELECT * FROM recent_enrichment_jobs;

-- Find lawyers needing specific enrichment:
-- SELECT id, full_name, completeness_score
-- FROM lawyer_staging
-- WHERE completeness_score < 80
--   AND NOT enrichment_layers @> '["justia"]'::jsonb
-- LIMIT 100;

-- ============================================================================
-- Grants (adjust as needed for your user)
-- ============================================================================

-- Grant permissions to scraper user
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO scraper;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO scraper;
-- GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO scraper;

-- ============================================================================
-- Done!
-- ============================================================================
