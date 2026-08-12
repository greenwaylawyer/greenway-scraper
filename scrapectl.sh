#!/usr/bin/env bash
# =============================================================================
# scrapectl.sh — Start / Stop the Greenway scraping services on demand
# =============================================================================
# The scraper stack (greenway_scraper + greenway_flaresolverr) is resource
# heavy (FlareSolverr runs a full Chrome browser). This script lets you bring
# the stack up only when you need to scrape, and shut it down afterwards.
#
# Usage:
#   ./scrapectl.sh start                 # bring stack up + start default worker
#   ./scrapectl.sh start --worker ...    # start with a custom worker command
#   ./scrapectl.sh stop                  # stop worker(s) + bring stack down
#   ./scrapectl.sh restart [--worker ..] # stop, then start
#   ./scrapectl.sh status                # show container + worker state
#   ./scrapectl.sh logs                  # tail scraper container logs
#
# The Laravel portal (postgres, nginx, api, redis, etc.) is intentionally NOT
# touched by this script.
# =============================================================================

set -euo pipefail

# Directory containing docker-compose.yml for the scraper stack.
APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
COMPOSE_FILE="$APP_DIR/docker-compose.yml"

SCRAPER_CONTAINER="greenway_scraper"
FLARESOLVERR_CONTAINER="greenway_flaresolverr"

# Default background worker launched on `start` (DB-as-queue scraping poller).
# Override with `--worker` or by setting WORKER_CMD before calling.
WORKER_CMD="${WORKER_CMD:-python run_enrichment.py --worker scraping --poll-interval 30}"

# Colors
GREEN='\033[0;32m'
BLUE='\033[0;34m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m'

log()   { echo -e "${BLUE}[scrapectl]${NC} $1"; }
ok()    { echo -e "${GREEN}  ✓${NC} $1"; }
warn()  { echo -e "${YELLOW}  !${NC} $1"; }
fail()  { echo -e "${RED}  ✗${NC} $1"; }

usage() {
    cat <<EOF
Usage: $(basename "$0") <command> [options]

Commands:
  start                 Start scraper stack and launch a background worker
  stop                  Stop worker(s) and shut down the scraper stack
  restart               Stop, then start (accepts --worker)
  status                Show container and worker status
  logs                  Follow scraper container logs

Options (start / restart):
  --worker CMD          Override the default worker command. CMD is passed
                        verbatim to: docker exec -d greenway_scraper <CMD>

Examples:
  ./scrapectl.sh start
  ./scrapectl.sh start --worker "python run_enrichment.py --worker all --poll-interval 60"
  ./scrapectl.sh start --worker "python run_enrichment.py --worker mvp_google_first --batch-size 200"
  ./scrapectl.sh stop
EOF
}

require_docker() {
    command -v docker >/dev/null 2>&1 || { fail "docker not found in PATH"; exit 1; }
    [ -f "$COMPOSE_FILE" ] || { fail "Compose file not found: $COMPOSE_FILE"; exit 1; }
}

compose() {
    docker compose -f "$COMPOSE_FILE" "$@"
}

container_running() {
    docker ps --format '{{.Names}}' | grep -qx "$1"
}

cmd_start() {
    local worker="$WORKER_CMD"

    while [ $# -gt 0 ]; do
        case "$1" in
            --worker)
                shift
                [ $# -gt 0 ] || { fail "--worker requires an argument"; exit 1; }
                worker="$1"
                ;;
            *)
                fail "Unknown option: $1"
                usage
                exit 1
                ;;
        esac
        shift
    done

    require_docker

    log "Starting scraper stack..."
    compose up -d
    ok "Containers started: $SCRAPER_CONTAINER, $FLARESOLVERR_CONTAINER"

    log "Waiting for FlareSolverr to be ready..."
    sleep 5

    if container_running "$SCRAPER_CONTAINER"; then
        log "Launching background worker..."
        docker exec -d "$SCRAPER_CONTAINER" $worker
        ok "Worker started: $worker"
    else
        warn "$SCRAPER_CONTAINER is not running — worker not started"
    fi

    log "Done. Use '$(basename "$0") status' to check, '$(basename "$0") logs' to follow output."
}

cmd_stop() {
    require_docker

    log "Stopping background workers (if any)..."
    if container_running "$SCRAPER_CONTAINER"; then
        docker exec "$SCRAPER_CONTAINER" pkill -f 'run_enrichment.py|run.py' 2>/dev/null || true
        ok "Worker processes stopped"
    else
        warn "$SCRAPER_CONTAINER not running — nothing to stop"
    fi

    log "Shutting down scraper stack..."
    compose down
    ok "Scraper stack stopped (portal services untouched)"
}

cmd_status() {
    require_docker

    echo ""
    echo -e "${BLUE}=== Containers ===${NC}"
    docker ps -a --format 'table {{.Names}}\t{{.Status}}\t{{.RunningFor}}' \
        --filter "name=$SCRAPER_CONTAINER" \
        --filter "name=$FLARESOLVERR_CONTAINER"

    echo ""
    echo -e "${BLUE}=== Background workers ===${NC}"
    if container_running "$SCRAPER_CONTAINER"; then
        local procs
        procs="$(docker exec "$SCRAPER_CONTAINER" ps aux 2>/dev/null | grep -E 'run_enrichment\.py|run\.py' | grep -v grep || true)"
        if [ -n "$procs" ]; then
            echo "$procs"
        else
            echo "No scraping workers running."
        fi
    else
        echo "$SCRAPER_CONTAINER is not running."
    fi
    echo ""
}

cmd_logs() {
    require_docker
    docker logs "$SCRAPER_CONTAINER" --tail 100 -f
}

main() {
    [ $# -ge 1 ] || { usage; exit 1; }

    local cmd="$1"
    shift

    case "$cmd" in
        start)
            cmd_start "$@"
            ;;
        stop)
            cmd_stop "$@"
            ;;
        restart)
            log "Restarting scraper stack..."
            cmd_stop
            cmd_start "$@"
            ;;
        status)
            cmd_status "$@"
            ;;
        logs)
            cmd_logs "$@"
            ;;
        -h|--help|help)
            usage
            ;;
        *)
            echo -e "${RED}Unknown command: $cmd${NC}"
            usage
            exit 1
            ;;
    esac
}

main "$@"
