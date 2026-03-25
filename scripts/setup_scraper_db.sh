#!/bin/bash
# ============================================================================
# Create Greenway Scraper Database
# ============================================================================
# This script creates the scraper database and runs the schema migrations.
#
# Usage:
#   ./scripts/setup_scraper_db.sh
#
# Environment variables (optional):
#   POSTGRES_HOST - PostgreSQL server host (default: 127.0.0.1)
#   POSTGRES_PORT - PostgreSQL server port (default: 5432)
#   POSTGRES_USER - PostgreSQL superuser (default: postgres)
#   POSTGRES_PASSWORD - PostgreSQL superuser password
# ============================================================================

set -e  # Exit on error

# Configuration
POSTGRES_HOST="${POSTGRES_HOST:-127.0.0.1}"
POSTGRES_PORT="${POSTGRES_PORT:-5432}"
POSTGRES_USER="${POSTGRES_USER:-postgres}"

SCRAPER_DB_NAME="greenway_scraper"
SCRAPER_DB_PORT="${SCRAPER_DB_PORT:-5433}"
SCRAPER_DB_USER="scraper"
SCRAPER_DB_PASSWORD="${SCRAPER_DB_PASSWORD:-scraper_secret}"

SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SCHEMA_FILE="$SCRIPT_DIR/create_scraper_database.sql"

echo "============================================================================"
echo "Greenway Scraper Database Setup"
echo "============================================================================"
echo ""
echo "This will create:"
echo "  - Database: $SCRAPER_DB_NAME"
echo "  - User: $SCRAPER_DB_USER"
echo "  - Schema: lawyer_enrichment, enrichment_batches, etc."
echo ""
echo "Connecting to PostgreSQL at $POSTGRES_HOST:$POSTGRES_PORT as $POSTGRES_USER"
echo ""

# Prompt for password if not set
if [ -z "$POSTGRES_PASSWORD" ]; then
    read -s -p "Enter PostgreSQL password for user '$POSTGRES_USER': " POSTGRES_PASSWORD
    echo ""
fi

export PGPASSWORD="$POSTGRES_PASSWORD"

echo ""
echo "Step 1: Creating database and user..."

# Create database and user
psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d postgres <<EOF
-- Drop existing database if requested
DO \$\$
BEGIN
    -- Optionally drop database (comment out if you want to keep existing data)
    -- DROP DATABASE IF EXISTS $SCRAPER_DB_NAME;

    -- Create database if not exists
    IF NOT EXISTS (SELECT FROM pg_database WHERE datname = '$SCRAPER_DB_NAME') THEN
        CREATE DATABASE $SCRAPER_DB_NAME;
        RAISE NOTICE 'Database $SCRAPER_DB_NAME created';
    ELSE
        RAISE NOTICE 'Database $SCRAPER_DB_NAME already exists';
    END IF;
END
\$\$;

-- Create user if not exists
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_user WHERE usename = '$SCRAPER_DB_USER') THEN
        CREATE USER $SCRAPER_DB_USER WITH PASSWORD '$SCRAPER_DB_PASSWORD';
        RAISE NOTICE 'User $SCRAPER_DB_USER created';
    ELSE
        RAISE NOTICE 'User $SCRAPER_DB_USER already exists';
    END IF;
END
\$\$;

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE $SCRAPER_DB_NAME TO $SCRAPER_DB_USER;
EOF

echo "✓ Database and user ready"
echo ""
echo "Step 2: Creating schema..."

# Run schema migration
psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$SCRAPER_DB_NAME" -f "$SCHEMA_FILE"

echo "✓ Schema created"
echo ""
echo "Step 3: Granting permissions..."

# Grant permissions on all tables
psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$POSTGRES_USER" -d "$SCRAPER_DB_NAME" <<EOF
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO $SCRAPER_DB_USER;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO $SCRAPER_DB_USER;
GRANT ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA public TO $SCRAPER_DB_USER;

-- Allow future tables to be accessible
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON TABLES TO $SCRAPER_DB_USER;
ALTER DEFAULT PRIVILEGES IN SCHEMA public GRANT ALL ON SEQUENCES TO $SCRAPER_DB_USER;
EOF

echo "✓ Permissions granted"
echo ""

# Test connection
echo "Step 4: Testing connection..."
export PGPASSWORD="$SCRAPER_DB_PASSWORD"

psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$SCRAPER_DB_USER" -d "$SCRAPER_DB_NAME" -c "\dt" > /dev/null 2>&1

if [ $? -eq 0 ]; then
    echo "✓ Connection test successful"
else
    echo "✗ Connection test failed"
    exit 1
fi

echo ""
echo "============================================================================"
echo "✓ Scraper database setup complete!"
echo "============================================================================"
echo ""
echo "Database connection details:"
echo "  Host: $POSTGRES_HOST"
echo "  Port: $POSTGRES_PORT"
echo "  Database: $SCRAPER_DB_NAME"
echo "  User: $SCRAPER_DB_USER"
echo ""
echo "Update your .env file with:"
echo "  SCRAPER_DB_HOST=$POSTGRES_HOST"
echo "  SCRAPER_DB_PORT=$POSTGRES_PORT"
echo "  SCRAPER_DB_NAME=$SCRAPER_DB_NAME"
echo "  SCRAPER_DB_USER=$SCRAPER_DB_USER"
echo "  SCRAPER_DB_PASSWORD=$SCRAPER_DB_PASSWORD"
echo ""
echo "Tables created:"
psql -h "$POSTGRES_HOST" -p "$POSTGRES_PORT" -U "$SCRAPER_DB_USER" -d "$SCRAPER_DB_NAME" -c "\dt"
echo ""
echo "You can now run the scraper with:"
echo "  python run.py --state california --layer state_bars"
echo ""
