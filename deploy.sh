#!/bin/bash
set -e

APP_DIR="/home/jobaer/greenway-scraper"
LOG_FILE="$APP_DIR/deploy.log"

log() {
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] $1" | tee -a "$LOG_FILE"
}

log "=== Scraper Deploy start ==="

cd "$APP_DIR"

log "Pulling latest code..."
if git ls-remote origin main > /dev/null 2>&1; then
    git fetch origin main
    git reset --hard origin/main
else
    log "WARNING: Could not reach GitHub. Skipping git pull."
fi

log "Rebuilding and restarting..."
# avvo_poller is a long-running Python process: the code is volume-mounted, but it
# only picks up changes when the container restarts. An in-flight Avvo batch is cut
# off; the autopilot fails it on its stall timeout.
docker compose build scraper avvo_poller
docker compose up -d --force-recreate scraper avvo_poller

log "Cleaning old images..."
docker image prune -f

log "=== Scraper Deploy complete ==="
