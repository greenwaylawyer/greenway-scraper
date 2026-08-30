#!/bin/bash
# =============================================================================
# Enrichment Monitoring Script
# =============================================================================
# Quick commands to check the health of Layer 3 & 4 enrichment pipeline.
#
# Usage:
#   ./monitor_enrichment.sh [command]
#
# Commands:
#   status        - Overall status summary
#   discovery     - Discovery success rates
#   scraping      - Scraping success rates
#   conflicts     - Profiles with data conflicts
#   candidates    - Profiles needing admin review
#   cost          - Layer 4 (Google Maps) cost tracking
#   alerts        - Active alert conditions
#   workers       - Worker performance (last 24h)
#   progress      - Enrichment progress by layer
# =============================================================================

# Database connection (adjust if needed)
DB_HOST="${SCRAPER_DB_HOST:-127.0.0.1}"
DB_PORT="${SCRAPER_DB_PORT:-5433}"
DB_NAME="${SCRAPER_DB_NAME:-${SCRAPER_DB_DATABASE:-greenway_scraper}}"
DB_USER="${SCRAPER_DB_USER:-${SCRAPER_DB_USERNAME:-scraper}}"
# Postgres container name differs between dev (greenway_postgres) and prod
# (greenway_postgres_prod). Try both.
DB_CONTAINER="${DB_CONTAINER:-greenway_postgres_prod}"
DB_CONTAINER_ALT="${DB_CONTAINER_ALT:-greenway_postgres}"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper function to run SQL
run_sql() {
    docker exec "$DB_CONTAINER" psql -U "$DB_USER" -d "$DB_NAME" -c "$1" 2>/dev/null || \
    docker exec "$DB_CONTAINER_ALT" psql -U "$DB_USER" -d "$DB_NAME" -c "$1" 2>/dev/null || \
    psql -h "$DB_HOST" -p "$DB_PORT" -U "$DB_USER" -d "$DB_NAME" -c "$1" 2>/dev/null
}

# Helper function for pretty headers
print_header() {
    echo ""
    echo -e "${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}"
    echo ""
}

# Command: Status Summary
cmd_status() {
    print_header "ENRICHMENT STATUS SUMMARY"
    
    run_sql "
    SELECT 
        'Total Profiles' as metric,
        COUNT(*)::text as value
    FROM lawyer_enrichment
    UNION ALL
    SELECT 
        'Avg Completeness' as metric,
        ROUND(AVG(completeness_score), 1)::text || '%' as value
    FROM lawyer_enrichment
    UNION ALL
    SELECT 
        'Pending Discovery' as metric,
        COUNT(*)::text as value
    FROM enrichment_source_requests
    WHERE discovery_status = 'pending'
    UNION ALL
    SELECT 
        'Pending Scraping' as metric,
        COUNT(*)::text as value
    FROM enrichment_source_requests
    WHERE scrape_status IN ('pending', 'queued')
    UNION ALL
    SELECT 
        'Needs Admin Review' as metric,
        COUNT(*)::text as value
    FROM enrichment_source_requests
    WHERE discovery_status = 'candidates'
    UNION ALL
    SELECT 
        'Unresolved Conflicts' as metric,
        COUNT(*)::text as value
    FROM lawyer_enrichment
    WHERE jsonb_array_length(COALESCE(merged_data->'_needs_review', '[]'::jsonb)) > 0;
    "
}

# Command: Discovery Success Rates
cmd_discovery() {
    print_header "DISCOVERY SUCCESS RATES"
    run_sql "SELECT * FROM v_discovery_success_rate;"
}

# Command: Scraping Success Rates
cmd_scraping() {
    print_header "SCRAPING SUCCESS RATES"
    run_sql "SELECT * FROM v_scraping_success_rate;"
}

# Command: Conflicts
cmd_conflicts() {
    print_header "PROFILES WITH DATA CONFLICTS"
    run_sql "
    SELECT 
        full_name,
        bar_number,
        license_state,
        jsonb_array_length(conflicting_fields) as field_count,
        updated_at
    FROM v_conflicts_needing_resolution
    ORDER BY updated_at DESC
    LIMIT 20;
    "
}

# Command: Candidates
cmd_candidates() {
    print_header "PROFILES NEEDING ADMIN REVIEW"
    run_sql "
    SELECT 
        source_key,
        COUNT(*) as count,
        MIN(request_created_at) as oldest_request
    FROM v_profiles_needing_review
    GROUP BY source_key;
    "
}

# Command: Cost Tracking
cmd_cost() {
    print_header "LAYER 4 COST TRACKING (LAST 7 DAYS)"
    run_sql "
    SELECT 
        batch_date,
        find_place_calls,
        place_details_calls,
        batch_count as batches,
        total_cost_usd as cost_usd
    FROM v_layer4_cost_tracking
    WHERE batch_date >= CURRENT_DATE - INTERVAL '7 days'
    ORDER BY batch_date DESC;
    "
    
    echo ""
    echo -e "${YELLOW}Total Cost (Last 7 Days):${NC}"
    run_sql "
    SELECT 
        '$' || ROUND(SUM(total_cost_usd), 2) as total_cost_usd
    FROM v_layer4_cost_tracking
    WHERE batch_date >= CURRENT_DATE - INTERVAL '7 days';
    "
}

# Command: Alerts
cmd_alerts() {
    print_header "ACTIVE ALERTS"
    
    ALERTS=$(run_sql "SELECT COUNT(*) FROM v_enrichment_alerts;" | grep -E "^[0-9]+" | xargs)
    
    if [ "$ALERTS" -eq 0 ]; then
        echo -e "${GREEN}✓ No alerts. System is healthy!${NC}"
    else
        echo -e "${RED}⚠ $ALERTS alert(s) detected:${NC}"
        echo ""
        run_sql "SELECT * FROM v_enrichment_alerts ORDER BY severity DESC;"
    fi
}

# Command: Worker Performance
cmd_workers() {
    print_header "WORKER PERFORMANCE (LAST 24 HOURS)"
    run_sql "
    SELECT 
        source_key,
        hour_bucket,
        profiles_scraped,
        success_rate_pct,
        profiles_per_minute
    FROM v_worker_performance
    ORDER BY hour_bucket DESC
    LIMIT 20;
    "
}

# Command: Progress
cmd_progress() {
    print_header "ENRICHMENT PROGRESS BY LAYER"
    run_sql "SELECT * FROM v_enrichment_progress_summary;"
}

# Command: Apify Avvo worker + batch status
cmd_apify() {
    print_header "APIFY AVVO WORKER STATUS"
    run_sql "
    SELECT worker_name, status,
           to_char(last_heartbeat_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI:SS') as last_heartbeat_utc,
           CASE WHEN last_heartbeat_at > NOW() - INTERVAL '90 seconds' THEN 'RUNNING' ELSE 'STOPPED' END as worker_state
    FROM worker_heartbeats
    WHERE worker_name = 'apify_avvo';
    "

    print_header "APIFY AVVO BATCHES (recent 10)"
    run_sql "
    SELECT
        substr(batch_id::text, 1, 8) as batch,
        status,
        (metadata->'apify_run'->>'state') as apify_state,
        (metadata->'apify_run'->>'apify_status') as apify_run_status,
        records_processed || '/' || total_records as progress,
        records_failed,
        to_char(started_at AT TIME ZONE 'UTC', 'MM-DD HH24:MI') as started
    FROM enrichment_batches
    WHERE layer_name = 'avvo'
    ORDER BY started_at DESC
    LIMIT 10;
    "
}

# Main command dispatcher
COMMAND="${1:-status}"

case $COMMAND in
    status)
        cmd_status
        ;;
    discovery)
        cmd_discovery
        ;;
    scraping)
        cmd_scraping
        ;;
    conflicts)
        cmd_conflicts
        ;;
    candidates)
        cmd_candidates
        ;;
    cost)
        cmd_cost
        ;;
    alerts)
        cmd_alerts
        ;;
    workers)
        cmd_workers
        ;;
    progress)
        cmd_progress
        ;;
    apify)
        cmd_apify
        ;;
    all)
        cmd_status
        cmd_alerts
        cmd_discovery
        cmd_scraping
        cmd_progress
        cmd_apify
        ;;
    *)
        echo "Unknown command: $COMMAND"
        echo ""
        echo "Available commands:"
        echo "  status      - Overall status summary"
        echo "  discovery   - Discovery success rates"
        echo "  scraping    - Scraping success rates"
        echo "  conflicts   - Profiles with data conflicts"
        echo "  candidates  - Profiles needing admin review"
        echo "  cost        - Layer 4 cost tracking"
        echo "  alerts      - Active alert conditions"
        echo "  workers     - Worker performance"
        echo "  progress    - Progress by layer"
        echo "  apify       - Apify Avvo worker + batch status"
        echo "  all         - Run all checks"
        exit 1
        ;;
esac

echo ""
