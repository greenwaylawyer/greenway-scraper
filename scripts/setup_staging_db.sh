#!/bin/bash

# ============================================================================
# Greenway Scraper - Staging Database Setup Script
# ============================================================================
# This script creates the staging database and applies the schema.
# ============================================================================

set -e  # Exit on error

echo "=========================================="
echo "Greenway Scraper - Staging DB Setup"
echo "=========================================="
echo ""

# Load environment variables if .env exists
if [ -f .env ]; then
    echo "Loading environment variables from .env..."
    export $(grep -v '^#' .env | xargs)
fi

# Database configuration
STAGING_DB_HOST="${STAGING_DB_HOST:-127.0.0.1}"
STAGING_DB_PORT="${STAGING_DB_PORT:-5433}"
STAGING_DB_NAME="${STAGING_DB_NAME:-greenway_staging}"
STAGING_DB_USER="${STAGING_DB_USER:-scraper}"
STAGING_DB_PASSWORD="${STAGING_DB_PASSWORD:-scraper_secret}"
POSTGRES_ADMIN_USER="${POSTGRES_ADMIN_USER:-postgres}"

echo "Configuration:"
echo "  Host: $STAGING_DB_HOST"
echo "  Port: $STAGING_DB_PORT"
echo "  Database: $STAGING_DB_NAME"
echo "  User: $STAGING_DB_USER"
echo ""

# Check if psql is available
if ! command -v psql &> /dev/null; then
    echo "❌ Error: psql command not found!"
    echo "   Please install PostgreSQL client tools."
    exit 1
fi

echo "Step 1: Creating database and user..."
echo "--------------------------------------"

# Create database and user (as postgres admin)
PGPASSWORD="${POSTGRES_PASSWORD:-}" psql -h "$STAGING_DB_HOST" -p "$STAGING_DB_PORT" -U "$POSTGRES_ADMIN_USER" -d postgres <<EOF
-- Create user if not exists
DO \$\$
BEGIN
    IF NOT EXISTS (SELECT FROM pg_user WHERE usename = '$STAGING_DB_USER') THEN
        CREATE USER $STAGING_DB_USER WITH PASSWORD '$STAGING_DB_PASSWORD';
    END IF;
END
\$\$;

-- Create database if not exists
SELECT 'CREATE DATABASE $STAGING_DB_NAME OWNER $STAGING_DB_USER'
WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = '$STAGING_DB_NAME')\gexec

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE $STAGING_DB_NAME TO $STAGING_DB_USER;
EOF

if [ $? -eq 0 ]; then
    echo "✅ Database and user created successfully"
else
    echo "❌ Failed to create database and user"
    exit 1
fi

echo ""
echo "Step 2: Applying schema..."
echo "--------------------------------------"

# Apply schema
PGPASSWORD="$STAGING_DB_PASSWORD" psql -h "$STAGING_DB_HOST" -p "$STAGING_DB_PORT" -U "$STAGING_DB_USER" -d "$STAGING_DB_NAME" -f scripts/create_staging_schema.sql

if [ $? -eq 0 ]; then
    echo "✅ Schema applied successfully"
else
    echo "❌ Failed to apply schema"
    exit 1
fi

echo ""
echo "Step 3: Verifying installation..."
echo "--------------------------------------"

# Verify tables exist
TABLE_COUNT=$(PGPASSWORD="$STAGING_DB_PASSWORD" psql -h "$STAGING_DB_HOST" -p "$STAGING_DB_PORT" -U "$STAGING_DB_USER" -d "$STAGING_DB_NAME" -t -c "SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE';")

echo "Tables created: $TABLE_COUNT"

if [ "$TABLE_COUNT" -ge 2 ]; then
    echo "✅ Verification passed"
else
    echo "❌ Verification failed: Expected at least 2 tables"
    exit 1
fi

echo ""
echo "Step 4: Testing connection..."
echo "--------------------------------------"

PGPASSWORD="$STAGING_DB_PASSWORD" psql -h "$STAGING_DB_HOST" -p "$STAGING_DB_PORT" -U "$STAGING_DB_USER" -d "$STAGING_DB_NAME" -c "SELECT 'Connection successful!' as status;"

echo ""
echo "=========================================="
echo "✅ Setup Complete!"
echo "=========================================="
echo ""
echo "Staging database is ready at:"
echo "  postgresql://$STAGING_DB_USER:***@$STAGING_DB_HOST:$STAGING_DB_PORT/$STAGING_DB_NAME"
echo ""
echo "Next steps:"
echo "  1. Run a scraper with --export-staging flag"
echo "  2. Check enrichment stats:"
echo "     psql -h $STAGING_DB_HOST -p $STAGING_DB_PORT -U $STAGING_DB_USER -d $STAGING_DB_NAME -c 'SELECT * FROM enrichment_stats;'"
echo ""
