#!/bin/bash

# Setup script for creating the scraper database via Docker
# This version runs psql commands inside the greenway_postgres Docker container

set -e

CONTAINER_NAME="greenway_postgres"
POSTGRES_USER="greenway"
POSTGRES_PASSWORD="secret"
DB_NAME="greenway_scraper"
DB_USER="scraper"
DB_PASSWORD="scraper123"

echo "============================================================================"
echo "Greenway Scraper Database Setup (Docker Mode)"
echo "============================================================================"
echo ""
echo "This will create:"
echo "  - Database: $DB_NAME"
echo "  - User: $DB_USER"
echo "  - Schema: lawyer_enrichment, enrichment_batches, etc."
echo ""

# Check if container is running
if ! docker ps --format '{{.Names}}' | grep -q "^${CONTAINER_NAME}$"; then
    echo "❌ Error: PostgreSQL container '$CONTAINER_NAME' is not running"
    echo "Please start it with: cd ../greenway_lawyer_portal && docker compose up -d postgres"
    exit 1
fi

echo "✅ Found running PostgreSQL container: $CONTAINER_NAME"
echo ""

# Step 1: Create database and user
echo "Step 1: Creating database and user..."
docker exec -i $CONTAINER_NAME psql -U $POSTGRES_USER -d postgres <<EOF
-- Drop existing database and user if they exist (for clean setup)
DROP DATABASE IF EXISTS $DB_NAME;
DROP USER IF EXISTS $DB_USER;

-- Create new user
CREATE USER $DB_USER WITH PASSWORD '$DB_PASSWORD';

-- Create new database
CREATE DATABASE $DB_NAME OWNER $DB_USER;

-- Grant privileges
GRANT ALL PRIVILEGES ON DATABASE $DB_NAME TO $DB_USER;
EOF

if [ $? -eq 0 ]; then
    echo "✅ Database and user created successfully"
else
    echo "❌ Failed to create database and user"
    exit 1
fi
echo ""

# Step 2: Run schema migration
echo "Step 2: Running schema migration..."
SCRIPT_DIR="$( cd "$( dirname "${BASH_SOURCE[0]}" )" && pwd )"
SQL_FILE="$SCRIPT_DIR/create_scraper_database.sql"

if [ ! -f "$SQL_FILE" ]; then
    echo "❌ Error: SQL file not found: $SQL_FILE"
    exit 1
fi

# Copy SQL file into container and execute it
docker cp "$SQL_FILE" $CONTAINER_NAME:/tmp/create_scraper_database.sql
docker exec -i $CONTAINER_NAME psql -U $DB_USER -d $DB_NAME -f /tmp/create_scraper_database.sql
docker exec -i $CONTAINER_NAME rm /tmp/create_scraper_database.sql

if [ $? -eq 0 ]; then
    echo "✅ Schema created successfully"
else
    echo "❌ Failed to create schema"
    exit 1
fi
echo ""

# Step 3: Verify tables
echo "Step 3: Verifying tables..."
TABLES=$(docker exec -i $CONTAINER_NAME psql -U $DB_USER -d $DB_NAME -t -c "
    SELECT table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
    ORDER BY table_name;
")

if [ -z "$TABLES" ]; then
    echo "❌ No tables found"
    exit 1
fi

echo "✅ Found tables:"
echo "$TABLES" | sed 's/^/     /'
echo ""

# Step 4: Test connection and show summary
echo "Step 4: Testing connection..."
docker exec -i $CONTAINER_NAME psql -U $DB_USER -d $DB_NAME -c "
    SELECT
        (SELECT COUNT(*) FROM lawyer_enrichment) as lawyer_count,
        (SELECT COUNT(*) FROM enrichment_batches) as batch_count,
        (SELECT COUNT(*) FROM enrichment_errors) as error_count;
"

if [ $? -eq 0 ]; then
    echo ""
    echo "============================================================================"
    echo "✅ Database setup completed successfully!"
    echo "============================================================================"
    echo ""
    echo "Connection details:"
    echo "  Host: 127.0.0.1"
    echo "  Port: 5433 (mapped from container's 5432)"
    echo "  Database: $DB_NAME"
    echo "  User: $DB_USER"
    echo "  Password: $DB_PASSWORD"
    echo ""
    echo "Update your .env file with:"
    echo "  SCRAPER_DB_HOST=127.0.0.1"
    echo "  SCRAPER_DB_PORT=5433"
    echo "  SCRAPER_DB_NAME=$DB_NAME"
    echo "  SCRAPER_DB_USER=$DB_USER"
    echo "  SCRAPER_DB_PASSWORD=$DB_PASSWORD"
    echo ""
else
    echo "❌ Connection test failed"
    exit 1
fi
