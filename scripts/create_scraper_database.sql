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

    -- Manual curation (admin-managed profile data — scraper will not overwrite curated fields)
    manually_curated BOOLEAN DEFAULT false,
    manually_curated_at TIMESTAMP,
    manually_curated_by VARCHAR(255),
    manually_curated_fields JSONB DEFAULT '[]'::jsonb,  -- array of field names that are locked

    -- Shortlist for priority promotion
    shortlisted BOOLEAN DEFAULT false,
    shortlisted_at TIMESTAMP,
    shortlisted_by VARCHAR(255),

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
CREATE INDEX IF NOT EXISTS idx_enrichment_curated ON lawyer_enrichment(manually_curated) WHERE manually_curated = true;
CREATE INDEX IF NOT EXISTS idx_enrichment_shortlisted ON lawyer_enrichment(shortlisted) WHERE shortlisted = true;

-- Performance indexes for the admin list page (sort / filter / search).
CREATE EXTENSION IF NOT EXISTS pg_trgm;
CREATE INDEX IF NOT EXISTS idx_enrichment_created_at ON lawyer_enrichment(created_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_last_enriched ON lawyer_enrichment(last_enriched_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_promoted_at ON lawyer_enrichment(promoted_at);
CREATE INDEX IF NOT EXISTS idx_enrichment_full_name ON lawyer_enrichment(full_name);
CREATE INDEX IF NOT EXISTS idx_enrichment_not_promoted ON lawyer_enrichment(id) WHERE promoted_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_enrichment_practice_areas ON lawyer_enrichment USING gin((merged_data->'practice_areas'));
CREATE INDEX IF NOT EXISTS idx_enrichment_full_name_trgm ON lawyer_enrichment USING gin(full_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_enrichment_bar_number_trgm ON lawyer_enrichment USING gin(bar_number gin_trgm_ops);

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
-- Table: enrichment_source_requests
-- ============================================================================
-- Tracks per-profile, per-source enrichment requests for Layers 3 and 4.
-- One row per (lawyer_enrichment_id, source_key) combination.
-- Coordinates the discovery → scraping → merge pipeline.
CREATE TABLE IF NOT EXISTS enrichment_source_requests (
    id                      BIGSERIAL PRIMARY KEY,
    lawyer_enrichment_id    BIGINT NOT NULL REFERENCES lawyer_enrichment(id) ON DELETE CASCADE,
    source_key              VARCHAR(50) NOT NULL,
    layer                   SMALLINT NOT NULL CHECK (layer IN (3, 4)),

    -- Discovery Stage
    source_profile_url      VARCHAR(500) NULL,
    discovery_status        VARCHAR(30) NOT NULL DEFAULT 'pending'
                            CHECK (discovery_status IN (
                                'pending',      -- Not yet attempted
                                'searching',    -- Auto-discovery in progress
                                'found',        -- Profile found (single high-confidence match)
                                'candidates',   -- Multiple matches; admin must pick
                                'manual',       -- URL set manually by admin; skip discovery
                                'not_found',    -- Searched exhaustively; nothing found
                                'skipped'       -- Admin explicitly skipped this source
                            )),
    discovery_method        VARCHAR(30) NULL,
    discovery_attempts      SMALLINT NOT NULL DEFAULT 0,
    discovery_candidates    JSONB NOT NULL DEFAULT '[]',
    discovery_completed_at  TIMESTAMP NULL,

    -- Scraping Stage
    scrape_status           VARCHAR(30) NOT NULL DEFAULT 'pending'
                            CHECK (scrape_status IN (
                                'pending',      -- Waiting for discovery to complete
                                'queued',       -- In the scraping job queue
                                'scraping',     -- Currently being scraped
                                'completed',    -- Successfully scraped and merged
                                'failed',       -- Scraping failed (see scrape_error)
                                'no_data'       -- Page found but no useful data extracted
                            )),

    scraped_data            JSONB NULL,
    scrape_error            TEXT NULL,
    scrape_attempts         SMALLINT NOT NULL DEFAULT 0,
    scraped_at              TIMESTAMP NULL,

    -- Merge Tracking
    merged_to_profile       BOOLEAN NOT NULL DEFAULT false,
    merged_at               TIMESTAMP NULL,

    -- Google Places Specific (Layer 4)
    place_id                VARCHAR(200) NULL,
    match_confidence        DECIMAL(3,2) NULL,

    -- Request Metadata
    requested_by            VARCHAR(255) NULL,
    priority                SMALLINT NOT NULL DEFAULT 0,
    batch_id                UUID NULL,
    notes                   TEXT NULL,

    created_at              TIMESTAMP NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMP NOT NULL DEFAULT NOW(),

    CONSTRAINT uq_lawyer_source UNIQUE (lawyer_enrichment_id, source_key)
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_esr_discovery_pending
    ON enrichment_source_requests(source_key, discovery_status)
    WHERE discovery_status IN ('pending', 'searching');

CREATE INDEX IF NOT EXISTS idx_esr_scrape_pending
    ON enrichment_source_requests(source_key, scrape_status, priority DESC)
    WHERE scrape_status IN ('pending', 'queued');

CREATE INDEX IF NOT EXISTS idx_esr_candidates
    ON enrichment_source_requests(discovery_status)
    WHERE discovery_status = 'candidates';

CREATE INDEX IF NOT EXISTS idx_esr_lawyer
    ON enrichment_source_requests(lawyer_enrichment_id);

CREATE INDEX IF NOT EXISTS idx_esr_priority
    ON enrichment_source_requests(priority DESC, created_at ASC);

CREATE INDEX IF NOT EXISTS idx_esr_batch
    ON enrichment_source_requests(batch_id)
    WHERE batch_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_esr_layer_source
    ON enrichment_source_requests(layer, source_key, scrape_status);

-- Trigger for updated_at
DROP TRIGGER IF EXISTS update_enrichment_source_requests_updated_at ON enrichment_source_requests;
CREATE TRIGGER update_enrichment_source_requests_updated_at
    BEFORE UPDATE ON enrichment_source_requests
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE enrichment_source_requests IS
    'Per-profile, per-source enrichment requests for Layers 3 and 4. Tracks the full lifecycle: discovery → scraping → merge.';
COMMENT ON COLUMN enrichment_source_requests.discovery_candidates IS
    'All candidate profile matches found during auto-discovery. Admin reviews when status=candidates. Format: [{url, name, location, bar_number, confidence, match_signals}]';
COMMENT ON COLUMN enrichment_source_requests.scraped_data IS
    'Raw data extracted from source profile. Shape is source-specific. Also written to lawyer_enrichment.raw_data_by_source[source_key].';
COMMENT ON COLUMN enrichment_source_requests.place_id IS
    'Google place_id (Layer 4 only). Stored permanently after first discovery to avoid repeat Find Place API calls.';

-- ============================================================================
-- Monitoring Views for Layer 3 & 4 Enrichment
-- ============================================================================

-- Discovery Success Rate by Source
CREATE OR REPLACE VIEW v_discovery_success_rate AS
SELECT 
    source_key,
    layer,
    COUNT(*) as total_requests,
    COUNT(*) FILTER (WHERE discovery_status = 'found') as found,
    COUNT(*) FILTER (WHERE discovery_status = 'candidates') as needs_review,
    COUNT(*) FILTER (WHERE discovery_status = 'not_found') as not_found,
    COUNT(*) FILTER (WHERE discovery_status = 'pending') as pending,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE discovery_status = 'found') / 
        NULLIF(COUNT(*) FILTER (WHERE discovery_status IN ('found', 'not_found', 'candidates')), 0),
        1
    ) as success_rate_pct
FROM enrichment_source_requests
GROUP BY source_key, layer
ORDER BY source_key;

COMMENT ON VIEW v_discovery_success_rate IS 
    'Discovery success rate metrics per source. Use to monitor if auto-discovery is working well (target: ≥60% success rate).';

-- Scraping Success Rate by Source
CREATE OR REPLACE VIEW v_scraping_success_rate AS
SELECT 
    source_key,
    layer,
    COUNT(*) as total_requests,
    COUNT(*) FILTER (WHERE scrape_status = 'completed') as completed,
    COUNT(*) FILTER (WHERE scrape_status = 'failed') as failed,
    COUNT(*) FILTER (WHERE scrape_status = 'no_data') as no_data,
    COUNT(*) FILTER (WHERE scrape_status IN ('pending', 'queued')) as queued,
    ROUND(
        100.0 * COUNT(*) FILTER (WHERE scrape_status = 'completed') / 
        NULLIF(COUNT(*) FILTER (WHERE scrape_status IN ('completed', 'failed', 'no_data')), 0),
        1
    ) as success_rate_pct,
    AVG(scrape_attempts) FILTER (WHERE scrape_status = 'completed') as avg_attempts_success,
    AVG(scrape_attempts) FILTER (WHERE scrape_status = 'failed') as avg_attempts_failed
FROM enrichment_source_requests
WHERE discovery_status IN ('found', 'manual')
GROUP BY source_key, layer
ORDER BY source_key;

COMMENT ON VIEW v_scraping_success_rate IS 
    'Scraping success rate metrics per source. Monitor for parse errors (target: ≤5% failed).';

-- Profiles Needing Admin Review
CREATE OR REPLACE VIEW v_profiles_needing_review AS
SELECT 
    le.id,
    le.full_name,
    le.bar_number,
    le.license_state,
    le.completeness_score,
    esr.source_key,
    esr.discovery_status,
    esr.discovery_candidates,
    esr.created_at as request_created_at
FROM enrichment_source_requests esr
JOIN lawyer_enrichment le ON le.id = esr.lawyer_enrichment_id
WHERE esr.discovery_status = 'candidates'
ORDER BY esr.created_at ASC;

COMMENT ON VIEW v_profiles_needing_review IS 
    'Profiles with multiple discovery candidates requiring admin selection.';

-- Conflict Resolution Queue
CREATE OR REPLACE VIEW v_conflicts_needing_resolution AS
SELECT 
    le.id,
    le.full_name,
    le.bar_number,
    le.license_state,
    le.completeness_score,
    le.merged_data->'_needs_review' as conflicting_fields,
    le.merged_data->'_conflicts' as conflict_details,
    le.updated_at
FROM lawyer_enrichment le
WHERE jsonb_array_length(COALESCE(le.merged_data->'_needs_review', '[]'::jsonb)) > 0
ORDER BY le.updated_at DESC;

COMMENT ON VIEW v_conflicts_needing_resolution IS 
    'Profiles with data conflicts from multiple sources requiring manual resolution.';

-- Layer 4 Cost Tracking
CREATE OR REPLACE VIEW v_layer4_cost_tracking AS
WITH daily_stats AS (
    SELECT 
        DATE(created_at) as batch_date,
        COUNT(*) FILTER (WHERE discovery_status IN ('found', 'manual')) as find_place_calls,
        COUNT(*) FILTER (WHERE scrape_status = 'completed') as place_details_calls,
        COUNT(DISTINCT batch_id) FILTER (WHERE batch_id IS NOT NULL) as batch_count
    FROM enrichment_source_requests
    WHERE source_key = 'google_maps'
      AND created_at >= CURRENT_DATE - INTERVAL '30 days'
    GROUP BY DATE(created_at)
)
SELECT 
    batch_date,
    find_place_calls,
    place_details_calls,
    batch_count,
    ROUND((find_place_calls / 1000.0 * 17), 2) as find_place_cost_usd,
    ROUND((place_details_calls / 1000.0 * 17), 2) as place_details_cost_usd,
    ROUND(((find_place_calls + place_details_calls) / 1000.0 * 17), 2) as total_cost_usd
FROM daily_stats
ORDER BY batch_date DESC;

COMMENT ON VIEW v_layer4_cost_tracking IS 
    'Daily Google Places API cost tracking. Monitor to stay within budget (target: <$50/day).';

-- Enrichment Progress Summary
CREATE OR REPLACE VIEW v_enrichment_progress_summary AS
SELECT 
    'Layer 1' as layer_name,
    1 as layer_num,
    COUNT(*) as profile_count,
    ROUND(AVG(completeness_score), 1) as avg_completeness
FROM lawyer_enrichment
WHERE enrichment_level >= 1

UNION ALL

SELECT 
    'Layer 2' as layer_name,
    2 as layer_num,
    COUNT(*) as profile_count,
    ROUND(AVG(completeness_score), 1) as avg_completeness
FROM lawyer_enrichment
WHERE enrichment_level >= 2

UNION ALL

SELECT 
    'Layer 3 - Justia' as layer_name,
    3 as layer_num,
    COUNT(*) as profile_count,
    ROUND(AVG(le.completeness_score), 1) as avg_completeness
FROM lawyer_enrichment le
JOIN enrichment_source_requests esr ON esr.lawyer_enrichment_id = le.id
WHERE esr.source_key = 'justia' AND esr.scrape_status = 'completed'

UNION ALL

SELECT 
    'Layer 3 - Avvo' as layer_name,
    3 as layer_num,
    COUNT(*) as profile_count,
    ROUND(AVG(le.completeness_score), 1) as avg_completeness
FROM lawyer_enrichment le
JOIN enrichment_source_requests esr ON esr.lawyer_enrichment_id = le.id
WHERE esr.source_key = 'avvo' AND esr.scrape_status = 'completed'

UNION ALL

SELECT 
    'Layer 4 - Google Maps' as layer_name,
    4 as layer_num,
    COUNT(*) as profile_count,
    ROUND(AVG(le.completeness_score), 1) as avg_completeness
FROM lawyer_enrichment le
JOIN enrichment_source_requests esr ON esr.lawyer_enrichment_id = le.id
WHERE esr.source_key = 'google_maps' AND esr.scrape_status = 'completed'

ORDER BY layer_num, layer_name;

COMMENT ON VIEW v_enrichment_progress_summary IS 
    'Overall enrichment progress across all layers. Use for dashboard metrics.';

-- Worker Performance Monitoring
CREATE OR REPLACE VIEW v_worker_performance AS
WITH hourly_stats AS (
    SELECT 
        source_key,
        DATE_TRUNC('hour', scraped_at) as hour_bucket,
        COUNT(*) as profiles_scraped,
        AVG(scrape_attempts) as avg_attempts,
        COUNT(*) FILTER (WHERE scrape_status = 'completed') as successful,
        COUNT(*) FILTER (WHERE scrape_status = 'failed') as failed
    FROM enrichment_source_requests
    WHERE scraped_at >= NOW() - INTERVAL '24 hours'
      AND scraped_at IS NOT NULL
    GROUP BY source_key, DATE_TRUNC('hour', scraped_at)
)
SELECT 
    source_key,
    hour_bucket,
    profiles_scraped,
    ROUND(avg_attempts, 2) as avg_attempts,
    successful,
    failed,
    ROUND(100.0 * successful / NULLIF(profiles_scraped, 0), 1) as success_rate_pct,
    ROUND(profiles_scraped::numeric / 60, 1) as profiles_per_minute
FROM hourly_stats
ORDER BY hour_bucket DESC, source_key;

COMMENT ON VIEW v_worker_performance IS 
    'Worker throughput and performance metrics over the last 24 hours. Monitor for rate limit issues.';

-- Alert Triggers View
CREATE OR REPLACE VIEW v_enrichment_alerts AS
SELECT 
    'High Discovery Failure Rate' as alert_type,
    'warning' as severity,
    source_key,
    CONCAT(
        'Discovery success rate is ',
        success_rate_pct,
        '% (threshold: 60%)'
    ) as message
FROM v_discovery_success_rate
WHERE success_rate_pct < 60 AND total_requests > 10

UNION ALL

SELECT 
    'High Scraping Failure Rate' as alert_type,
    'error' as severity,
    source_key,
    CONCAT(
        'Scraping success rate is ',
        success_rate_pct,
        '% (threshold: 90%)'
    ) as message
FROM v_scraping_success_rate
WHERE success_rate_pct < 90 AND total_requests > 10

UNION ALL

SELECT 
    'Google Places Cost Alert' as alert_type,
    'warning' as severity,
    'google_maps' as source_key,
    CONCAT(
        'Daily cost is $',
        total_cost_usd,
        ' (threshold: $50)'
    ) as message
FROM v_layer4_cost_tracking
WHERE batch_date = CURRENT_DATE AND total_cost_usd > 50

UNION ALL

SELECT 
    'Unresolved Candidates Backlog' as alert_type,
    'info' as severity,
    source_key as source_key,
    CONCAT(
        COUNT(*),
        ' profiles awaiting admin review'
    ) as message
FROM enrichment_source_requests
WHERE discovery_status = 'candidates'
GROUP BY source_key
HAVING COUNT(*) > 20

ORDER BY severity DESC, alert_type;

COMMENT ON VIEW v_enrichment_alerts IS 
    'Automatic alert triggers for monitoring dashboard. Query regularly to check for issues.';

-- Google-first MVP funnel metrics
CREATE OR REPLACE VIEW v_google_first_discovery_funnel AS
SELECT
    COALESCE(license_state, state) AS state_code,
    COUNT(*) FILTER (
        WHERE COALESCE((raw_data_by_source ? 'google_discovery'), false)
    ) AS discovered,
    COUNT(*) FILTER (
        WHERE COALESCE((merged_data->>'google_discovery_selected')::boolean, false) = true
    ) AS selected,
    COUNT(*) FILTER (
        WHERE raw_data_by_source ? 'justia'
           OR raw_data_by_source ? 'avvo'
    ) AS enriched_layer3,
    COUNT(*) FILTER (
        WHERE ready_for_promotion = true
    ) AS ready_for_promotion,
    COUNT(*) FILTER (
        WHERE manual_review_required = true
    ) AS manual_review_queue
FROM lawyer_enrichment
GROUP BY COALESCE(license_state, state)
ORDER BY discovered DESC;

COMMENT ON VIEW v_google_first_discovery_funnel IS
    'Google-first discovery -> selection -> enrichment -> publish funnel metrics by state.';

CREATE OR REPLACE VIEW v_google_first_quality_metrics AS
SELECT
    ROUND(
        100.0 * COUNT(*) FILTER (
            WHERE COALESCE((merged_data->>'google_discovery_selected')::boolean, false) = true
        ) / NULLIF(COUNT(*) FILTER (WHERE raw_data_by_source ? 'google_discovery'), 0),
        2
    ) AS selection_rate_pct,
    ROUND(
        100.0 * COUNT(*) FILTER (
            WHERE ready_for_promotion = true
        ) / NULLIF(COUNT(*) FILTER (
            WHERE COALESCE((merged_data->>'google_discovery_selected')::boolean, false) = true
        ), 0),
        2
    ) AS publish_pass_rate_pct,
    ROUND(AVG(completeness_score), 2) AS avg_completeness,
    ROUND(AVG(google_rating)::numeric, 2) AS avg_google_rating
FROM lawyer_enrichment
WHERE COALESCE((merged_data->>'google_discovery_selected')::boolean, false) = true;

COMMENT ON VIEW v_google_first_quality_metrics IS
    'Aggregate quality metrics for Google-first selected profiles.';

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
-- Batch progress aggregation
-- ============================================================================
-- Keeps enrichment_batches counters in sync with enrichment_source_requests so
-- per-profile bulk actions (e.g. "Request Google Maps Enrichment") are tracked
-- live on the Batches admin page without a dedicated worker.
CREATE OR REPLACE FUNCTION sync_batch_from_source_requests() RETURNS trigger AS $$
DECLARE
    b_id uuid := COALESCE(NEW.batch_id, OLD.batch_id);
BEGIN
    IF b_id IS NULL THEN
        RETURN COALESCE(NEW, OLD);
    END IF;

    UPDATE enrichment_batches b
    SET
        total_records = GREATEST(b.total_records, sub.total),
        records_processed = sub.processed,
        records_created = sub.completed,
        records_failed = sub.failed,
        progress_percent = CASE WHEN sub.total > 0 THEN ROUND((sub.processed::numeric / sub.total) * 100, 2) ELSE 0 END,
        status = CASE
            WHEN sub.total > 0 AND sub.processed >= sub.total THEN 'completed'
            WHEN sub.total > 0 AND sub.failed >= sub.total THEN 'failed'
            ELSE 'running'
        END,
        completed_at = CASE
            WHEN sub.total > 0 AND sub.processed >= sub.total THEN NOW()
            ELSE b.completed_at
        END,
        updated_at = NOW()
    FROM (
        SELECT
            COUNT(*)::int AS total,
            COUNT(*) FILTER (WHERE scrape_status IN ('completed', 'no_data')
                                  OR discovery_status IN ('not_found', 'skipped'))::int AS processed,
            COUNT(*) FILTER (WHERE scrape_status = 'completed')::int AS completed,
            COUNT(*) FILTER (WHERE scrape_status = 'failed')::int AS failed
        FROM enrichment_source_requests
        WHERE batch_id = b_id
    ) sub
    WHERE b.batch_id = b_id;

    RETURN COALESCE(NEW, OLD);
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_source_requests_batch ON enrichment_source_requests;
CREATE TRIGGER trg_source_requests_batch
    AFTER INSERT OR UPDATE OR DELETE ON enrichment_source_requests
    FOR EACH ROW
    EXECUTE FUNCTION sync_batch_from_source_requests();

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
