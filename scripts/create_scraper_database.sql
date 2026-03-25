-- ============================================================================
-- Greenway Scraper Database Schema
-- ============================================================================
-- This database stores raw scraped data and tracks enrichment progress.
-- Profiles are enriched through multiple layers until they reach the
-- completeness threshold for promotion to the portal database.
--
-- Database: greenway_scraper (separate from portal database)
-- ============================================================================

-- Create database (run as postgres superuser)
-- CREATE DATABASE greenway_scraper;
-- CREATE USER scraper WITH PASSWORD 'scraper_secret';
-- GRANT ALL PRIVILEGES ON DATABASE greenway_scraper TO scraper;

-- Connect to the database before running the rest
-- \c greenway_scraper

-- ============================================================================
-- Table: lawyer_enrichment
-- ============================================================================
-- Stores lawyer profiles with enrichment tracking
CREATE TABLE IF NOT EXISTS lawyer_enrichment (
    id BIGSERIAL PRIMARY KEY,

    -- Unique identifier for deduplication
    fingerprint VARCHAR(64) UNIQUE NOT NULL,

    -- Enrichment tracking
    enrichment_layers JSONB DEFAULT '[]'::jsonb NOT NULL,
    -- Example: ["state_bars", "justia", "google_places"]

    -- Multi-level enrichment tracking
    enrichment_level SMALLINT DEFAULT 0 CHECK (enrichment_level >= 0 AND enrichment_level <= 5),
    level_1_completed_at TIMESTAMP,  -- List scraping completed
    level_2_completed_at TIMESTAMP,  -- Detail page scraping completed
    level_3_completed_at TIMESTAMP,  -- Multi-source enrichment completed
    level_4_completed_at TIMESTAMP,  -- Google reviews completed
    level_5_completed_at TIMESTAMP,  -- Reserved for future use

    completeness_score SMALLINT DEFAULT 0 CHECK (completeness_score >= 0 AND completeness_score <= 100),
    last_enriched_at TIMESTAMP,

    -- Promotion control
    ready_for_promotion BOOLEAN DEFAULT false,
    promoted_at TIMESTAMP,
    promoted_to_portal_id UUID,  -- Reference to lawyers.id in portal DB

    -- Raw data from each source (preserved for auditing)
    raw_data_by_source JSONB DEFAULT '{}'::jsonb NOT NULL,
    -- Example structure:
    -- {
    --   "calbar": {
    --     "scraped_at": "2026-03-21T10:30:00Z",
    --     "data": { "full_name": "John Doe", "bar_number": "123456", ... }
    --   },
    --   "justia": {
    --     "scraped_at": "2026-03-22T14:20:00Z",
    --     "data": { "bio": "Attorney since 2010", "practice_areas": [...], ... }
    --   }
    -- }

    -- Merged/normalized data (best from all sources)
    merged_data JSONB NOT NULL,
    -- This contains the "best" value for each field based on merge strategy
    -- Each field can track which source it came from via field_source keys

    -- Normalized fields (extracted from merged_data for efficient querying)
    full_name VARCHAR(200),
    first_name VARCHAR(100),
    last_name VARCHAR(100),
    bar_number VARCHAR(50),
    license_state CHAR(2),
    license_status VARCHAR(30),
    admission_date DATE,
    firm_name VARCHAR(200),
    city VARCHAR(100),
    state CHAR(2),

    -- Quality control
    promotion_blocked BOOLEAN DEFAULT false,
    promotion_blocked_reason TEXT,
    manual_review_required BOOLEAN DEFAULT false,
    manual_review_reason TEXT,

    -- Timestamps
    created_at TIMESTAMP DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW() NOT NULL
);

-- Indexes for efficient querying
CREATE INDEX IF NOT EXISTS idx_enrichment_completeness ON lawyer_enrichment(completeness_score);
CREATE INDEX IF NOT EXISTS idx_enrichment_level ON lawyer_enrichment(enrichment_level);
CREATE INDEX IF NOT EXISTS idx_enrichment_ready ON lawyer_enrichment(ready_for_promotion) WHERE ready_for_promotion = true;
CREATE INDEX IF NOT EXISTS idx_enrichment_layers ON lawyer_enrichment USING gin(enrichment_layers);
CREATE INDEX IF NOT EXISTS idx_enrichment_state ON lawyer_enrichment(license_state);
CREATE INDEX IF NOT EXISTS idx_enrichment_name ON lawyer_enrichment(last_name, first_name);
CREATE INDEX IF NOT EXISTS idx_enrichment_bar ON lawyer_enrichment(bar_number);
CREATE INDEX IF NOT EXISTS idx_enrichment_updated ON lawyer_enrichment(updated_at);

-- ============================================================================
-- Table: enrichment_batches
-- ============================================================================
-- Tracks enrichment batch operations
CREATE TABLE IF NOT EXISTS enrichment_batches (
    id BIGSERIAL PRIMARY KEY,
    batch_id UUID UNIQUE NOT NULL,
    layer_name VARCHAR(50) NOT NULL,
    state VARCHAR(2),  -- US state code (e.g., 'CA', 'NY')

    -- Multi-level enrichment tracking
    enrichment_level SMALLINT DEFAULT 1 CHECK (enrichment_level >= 1 AND enrichment_level <= 5),
    source_batch_id UUID,  -- Reference to parent batch (for chaining levels)

    -- Timing
    started_at TIMESTAMP NOT NULL DEFAULT NOW(),
    completed_at TIMESTAMP,
    duration_seconds INT,

    -- Status
    status VARCHAR(20) DEFAULT 'running' CHECK (status IN ('running', 'completed', 'failed', 'cancelled')),

    -- Metrics
    total_records INT DEFAULT 0,
    records_processed INT DEFAULT 0,
    records_created INT DEFAULT 0,
    records_updated INT DEFAULT 0,
    records_skipped INT DEFAULT 0,
    records_failed INT DEFAULT 0,

    -- Progress (for monitoring UI)
    progress_percent DECIMAL(5,2) DEFAULT 0,
    current_record_index INT DEFAULT 0,

    -- Configuration snapshot
    config_snapshot JSONB,

    -- Error tracking
    error_log TEXT,
    error_count INT DEFAULT 0,

    -- Metadata
    metadata JSONB,

    created_at TIMESTAMP DEFAULT NOW() NOT NULL,
    updated_at TIMESTAMP DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_batches_layer ON enrichment_batches(layer_name, status);
CREATE INDEX IF NOT EXISTS idx_batches_level ON enrichment_batches(enrichment_level);
CREATE INDEX IF NOT EXISTS idx_batches_source ON enrichment_batches(source_batch_id) WHERE source_batch_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_batches_status ON enrichment_batches(status);
CREATE INDEX IF NOT EXISTS idx_batches_started ON enrichment_batches(started_at DESC);

-- ============================================================================
-- Table: enrichment_errors
-- ============================================================================
-- Detailed error logging for troubleshooting
CREATE TABLE IF NOT EXISTS enrichment_errors (
    id BIGSERIAL PRIMARY KEY,
    batch_id UUID NOT NULL REFERENCES enrichment_batches(batch_id) ON DELETE CASCADE,

    -- Error details
    error_type VARCHAR(100),
    error_message TEXT NOT NULL,
    error_stacktrace TEXT,

    -- Context
    lawyer_id BIGINT REFERENCES lawyer_enrichment(id) ON DELETE SET NULL,
    lawyer_fingerprint VARCHAR(64),
    layer_name VARCHAR(50),

    -- Data snapshot at time of error
    context_data JSONB,

    -- Resolution
    resolved BOOLEAN DEFAULT false,
    resolved_at TIMESTAMP,
    resolution_notes TEXT,

    created_at TIMESTAMP DEFAULT NOW() NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_errors_batch ON enrichment_errors(batch_id);
CREATE INDEX IF NOT EXISTS idx_errors_type ON enrichment_errors(error_type);
CREATE INDEX IF NOT EXISTS idx_errors_unresolved ON enrichment_errors(resolved) WHERE resolved = false;

-- ============================================================================
-- Table: promotion_log
-- ============================================================================
-- Audit trail of promotions to portal database
CREATE TABLE IF NOT EXISTS promotion_log (
    id BIGSERIAL PRIMARY KEY,

    -- Source
    lawyer_enrichment_id BIGINT NOT NULL REFERENCES lawyer_enrichment(id),
    fingerprint VARCHAR(64) NOT NULL,

    -- Destination
    portal_lawyer_id UUID NOT NULL,

    -- Promotion details
    completeness_score SMALLINT NOT NULL,
    enrichment_layers JSONB NOT NULL,

    -- Timing
    promoted_at TIMESTAMP DEFAULT NOW() NOT NULL,

    -- Data snapshot
    promoted_data JSONB NOT NULL,

    -- Result
    success BOOLEAN DEFAULT true,
    error_message TEXT
);

CREATE INDEX IF NOT EXISTS idx_promotion_log_enrichment ON promotion_log(lawyer_enrichment_id);
CREATE INDEX IF NOT EXISTS idx_promotion_log_portal ON promotion_log(portal_lawyer_id);
CREATE INDEX IF NOT EXISTS idx_promotion_log_promoted ON promotion_log(promoted_at DESC);

-- ============================================================================
-- Views for monitoring
-- ============================================================================

-- Current enrichment statistics
CREATE OR REPLACE VIEW v_enrichment_stats AS
SELECT
    license_state,
    COUNT(*) as total_profiles,
    AVG(completeness_score)::DECIMAL(5,2) as avg_completeness,
    COUNT(*) FILTER (WHERE completeness_score >= 80) as ready_for_promotion,
    COUNT(*) FILTER (WHERE completeness_score < 40) as needs_enrichment,
    COUNT(*) FILTER (WHERE manual_review_required = true) as needs_review,
    COUNT(*) FILTER (WHERE promotion_blocked = true) as blocked
FROM lawyer_enrichment
WHERE promoted_at IS NULL
GROUP BY license_state
ORDER BY total_profiles DESC;

-- Layer completion status
CREATE OR REPLACE VIEW v_layer_completion AS
SELECT
    layer_name,
    COUNT(*) as profiles_with_layer,
    AVG(completeness_score)::DECIMAL(5,2) as avg_completeness_after_layer
FROM lawyer_enrichment,
     jsonb_array_elements_text(enrichment_layers) as layer_name
WHERE promoted_at IS NULL
GROUP BY layer_name
ORDER BY layer_name;

-- Recent batch activity
CREATE OR REPLACE VIEW v_recent_batches AS
SELECT
    batch_id,
    layer_name,
    status,
    started_at,
    completed_at,
    duration_seconds,
    records_processed,
    records_created,
    records_updated,
    records_failed,
    progress_percent
FROM enrichment_batches
ORDER BY started_at DESC
LIMIT 50;

-- ============================================================================
-- Functions
-- ============================================================================

-- Update timestamp trigger function
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- Create triggers for updated_at
DROP TRIGGER IF EXISTS update_lawyer_enrichment_updated_at ON lawyer_enrichment;
CREATE TRIGGER update_lawyer_enrichment_updated_at
    BEFORE UPDATE ON lawyer_enrichment
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

DROP TRIGGER IF EXISTS update_enrichment_batches_updated_at ON enrichment_batches;
CREATE TRIGGER update_enrichment_batches_updated_at
    BEFORE UPDATE ON enrichment_batches
    FOR EACH ROW
    EXECUTE FUNCTION update_updated_at_column();

-- ============================================================================
-- Sample queries for monitoring
-- ============================================================================

-- Find profiles ready for promotion
-- SELECT id, fingerprint, full_name, completeness_score, enrichment_layers
-- FROM lawyer_enrichment
-- WHERE ready_for_promotion = true AND promoted_at IS NULL
-- ORDER BY completeness_score DESC
-- LIMIT 100;

-- Profiles needing more enrichment (below 80%)
-- SELECT license_state, COUNT(*) as count, AVG(completeness_score) as avg_score
-- FROM lawyer_enrichment
-- WHERE completeness_score < 80 AND promoted_at IS NULL
-- GROUP BY license_state;

-- Latest enrichment activity
-- SELECT * FROM v_recent_batches;

-- Overall statistics
-- SELECT * FROM v_enrichment_stats;

COMMENT ON DATABASE greenway_scraper IS 'Greenway Scraper enrichment database - stores profiles through multi-layer enrichment pipeline';
COMMENT ON TABLE lawyer_enrichment IS 'Lawyer profiles with enrichment tracking - promoted to portal DB when completeness >= threshold';
COMMENT ON TABLE enrichment_batches IS 'Tracks enrichment batch operations for monitoring and debugging';
COMMENT ON TABLE enrichment_errors IS 'Detailed error log for troubleshooting enrichment failures';
COMMENT ON TABLE promotion_log IS 'Audit trail of promotions from scraper DB to portal DB';
