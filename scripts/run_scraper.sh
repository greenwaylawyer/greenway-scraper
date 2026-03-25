#!/bin/bash
# On-Demand Scraper Execution Script

set -e

CONTAINER_NAME="greenway_scraper"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo -e "${BLUE}==================================================================${NC}"
echo -e "${BLUE}       Greenway Scraper - On-Demand Execution${NC}"
echo -e "${BLUE}==================================================================${NC}"
echo ""

# Check if container exists and is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo -e "${YELLOW}⚠️  Container '$CONTAINER_NAME' is not running${NC}"
    echo ""
    echo "Starting container..."
    docker compose up -d scraper
    echo ""
    sleep 2
fi

echo -e "${GREEN}✅ Container is running${NC}"
echo ""

# Parse arguments
STATE="${1:-california}"
LAYER="${2:-state_bars}"
EXPORT_SCRAPER="${3:-yes}"
EXPORT_JSON="${4:-no}"
HEADLESS="${5:-true}"

echo "Configuration:"
echo "  State: $STATE"
echo "  Layer: $LAYER"
echo "  Export to DB: $EXPORT_SCRAPER"
echo "  Export to JSON: $EXPORT_JSON"
echo "  Headless: $HEADLESS"
echo ""

# Build command
CMD="python run.py --state $STATE --layer $LAYER"

if [ "$EXPORT_SCRAPER" = "yes" ]; then
    CMD="$CMD --export-scraper"
fi

if [ "$EXPORT_JSON" = "yes" ]; then
    CMD="$CMD --export-json"
fi

CMD="$CMD --headless $HEADLESS"

echo -e "${BLUE}Executing: $CMD${NC}"
echo ""
echo "-------------------------------------------------------------------"
echo ""

# Execute scraper
docker exec -it $CONTAINER_NAME $CMD

EXIT_CODE=$?

echo ""
echo "-------------------------------------------------------------------"
echo ""

if [ $EXIT_CODE -eq 0 ]; then
    echo -e "${GREEN}✅ Scraper completed successfully${NC}"
else
    echo -e "${YELLOW}❌ Scraper failed with exit code: $EXIT_CODE${NC}"
fi

echo ""
echo "View results:"
echo "  Database: docker exec greenway_postgres psql -U scraper -d greenway_scraper -c 'SELECT COUNT(*) FROM lawyer_enrichment;'"
echo "  JSON: ls -lh output/"
echo ""
