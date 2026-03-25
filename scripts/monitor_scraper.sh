#!/bin/bash
# Scraper Monitoring Script - Quick Status Check

set -e

CONTAINER_NAME="greenway_scraper"
DB_CONTAINER="greenway_postgres"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

echo -e "${BLUE}==================================================================${NC}"
echo -e "${BLUE}       Greenway Scraper - Live Monitoring${NC}"
echo -e "${BLUE}==================================================================${NC}"
echo ""

# Check container status
if docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo -e "${GREEN}✅ Scraper Container: RUNNING${NC}"
else
    echo -e "${RED}❌ Scraper Container: STOPPED${NC}"
    exit 1
fi

if docker ps --format '{{.Names}}' | grep -q "^${DB_CONTAINER}$"; then
    echo -e "${GREEN}✅ Database Container: RUNNING${NC}"
else
    echo -e "${RED}❌ Database Container: STOPPED${NC}"
    exit 1
fi

echo ""
echo -e "${BLUE}------------------------------------------------------------------${NC}"
echo -e "${BLUE}ENRICHMENT STATISTICS${NC}"
echo -e "${BLUE}------------------------------------------------------------------${NC}"

docker exec $DB_CONTAINER psql -U scraper -d greenway_scraper << 'EOF'
\x
SELECT
    COUNT(*) as "Total Lawyers",
    ROUND(AVG(completeness_score), 1) || '%' as "Avg Completeness",
    COUNT(*) FILTER (WHERE completeness_score >= 80) as "Ready (≥80%)",
    COUNT(*) FILTER (WHERE completeness_score < 60) as "Low (<60%)",
    COUNT(DISTINCT license_state) as "States Covered",
    MAX(last_enriched_at) as "Last Enrichment"
FROM lawyer_enrichment;
EOF

echo ""
echo -e "${BLUE}------------------------------------------------------------------${NC}"
echo -e "${BLUE}RECENT BATCHES (Last 5)${NC}"
echo -e "${BLUE}------------------------------------------------------------------${NC}"

docker exec $DB_CONTAINER psql -U scraper -d greenway_scraper << 'EOF'
SELECT
    LEFT(batch_id::text, 8) as "Batch",
    layer_name as "Layer",
    state as "St",
    status as "Status",
    total_records as "Total",
    records_processed as "Processed",
    records_failed as "Failed",
    TO_CHAR(started_at, 'MM-DD HH24:MI') as "Started",
    CASE
        WHEN completed_at IS NOT NULL
        THEN EXTRACT(EPOCH FROM (completed_at - started_at))::int || 's'
        ELSE 'Running...'
    END as "Duration"
FROM enrichment_batches
ORDER BY started_at DESC
LIMIT 5;
EOF

echo ""
echo -e "${BLUE}------------------------------------------------------------------${NC}"
echo -e "${BLUE}ENRICHMENT PROGRESS BY LAYER${NC}"
echo -e "${BLUE}------------------------------------------------------------------${NC}"

docker exec $DB_CONTAINER psql -U scraper -d greenway_scraper << 'EOF'
SELECT
    COALESCE(jsonb_object_keys(enrichment_layers), 'No layers') as "Layer",
    COUNT(*) as "Lawyers",
    ROUND(AVG(completeness_score), 1) || '%' as "Avg Score"
FROM lawyer_enrichment
GROUP BY jsonb_object_keys(enrichment_layers)
ORDER BY COUNT(*) DESC;
EOF

echo ""
echo -e "${BLUE}------------------------------------------------------------------${NC}"
echo -e "${BLUE}RECENT ERRORS (Last 5)${NC}"
echo -e "${BLUE}------------------------------------------------------------------${NC}"

docker exec $DB_CONTAINER psql -U scraper -d greenway_scraper << 'EOF'
SELECT
    layer_name as "Layer",
    state as "St",
    LEFT(error_message, 50) as "Error Message",
    TO_CHAR(occurred_at, 'MM-DD HH24:MI') as "When"
FROM enrichment_errors
ORDER BY occurred_at DESC
LIMIT 5;
EOF

echo ""
echo -e "${BLUE}------------------------------------------------------------------${NC}"
echo -e "${BLUE}TOP 10 MOST COMPLETE LAWYERS${NC}"
echo -e "${BLUE}------------------------------------------------------------------${NC}"

docker exec $DB_CONTAINER psql -U scraper -d greenway_scraper << 'EOF'
SELECT
    full_name as "Name",
    bar_number as "Bar #",
    license_state as "St",
    completeness_score || '%' as "Score",
    CASE WHEN ready_for_promotion THEN '✓' ELSE '✗' END as "Ready"
FROM lawyer_enrichment
ORDER BY completeness_score DESC, full_name
LIMIT 10;
EOF

echo ""
echo -e "${GREEN}==================================================================${NC}"
echo -e "${GREEN}Monitoring complete. Run 'watch -n 5 ./scripts/monitor_scraper.sh' for live updates${NC}"
echo -e "${GREEN}==================================================================${NC}"
echo ""

# Show quick actions
echo "Quick Actions:"
echo "  1. Run scraper:    ./scripts/run_scraper.sh california state_bars"
echo "  2. View logs:      docker logs greenway_scraper -f"
echo "  3. Stop scraper:   docker stop greenway_scraper"
echo "  4. Start scraper:  docker compose up -d scraper"
echo ""
