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
docker compose build scraper
docker compose up -d --force-recreate scraper

log "Cleaning old images..."
docker image prune -f

log "=== Scraper Deploy complete ==="
