#!/bin/bash
# View and manage scraper checkpoints

set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CHECKPOINT_DIR="$SCRIPT_DIR/../checkpoints"

# Colors for output
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${BLUE}     Greenway Scraper - Checkpoint Manager     ${NC}"
echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

if [ "$1" = "list" ] || [ -z "$1" ]; then
    echo -e "${GREEN}📍 Available Checkpoints:${NC}"
    echo ""

    if [ ! -d "$CHECKPOINT_DIR" ] || [ -z "$(ls -A $CHECKPOINT_DIR 2>/dev/null)" ]; then
        echo -e "${YELLOW}No checkpoints found${NC}"
        exit 0
    fi

    for checkpoint_file in "$CHECKPOINT_DIR"/*.json; do
        if [ -f "$checkpoint_file" ]; then
            state=$(basename "$checkpoint_file" .json)
            echo -e "${BLUE}State: ${GREEN}${state}${NC}"

            # Extract and display checkpoint info using jq if available, otherwise cat
            if command -v jq &> /dev/null; then
                echo "  Last Page: $(jq -r '.last_page' "$checkpoint_file")"
                echo "  Timestamp: $(jq -r '.timestamp' "$checkpoint_file")"
                echo "  Stats: $(jq -c '.stats' "$checkpoint_file")"
            else
                cat "$checkpoint_file" | sed 's/^/  /'
            fi
            echo ""
        fi
    done

elif [ "$1" = "clear" ]; then
    if [ -z "$2" ]; then
        echo -e "${RED}Error: Please specify a state to clear${NC}"
        echo "Usage: $0 clear <state>"
        echo "Example: $0 clear california"
        exit 1
    fi

    state="$2"
    checkpoint_file="$CHECKPOINT_DIR/${state}.json"

    if [ -f "$checkpoint_file" ]; then
        rm "$checkpoint_file"
        echo -e "${GREEN}✓ Checkpoint for ${state} cleared${NC}"
    else
        echo -e "${YELLOW}No checkpoint found for ${state}${NC}"
    fi

elif [ "$1" = "clear-all" ]; then
    if [ -d "$CHECKPOINT_DIR" ]; then
        rm -f "$CHECKPOINT_DIR"/*.json
        echo -e "${GREEN}✓ All checkpoints cleared${NC}"
    else
        echo -e "${YELLOW}No checkpoints directory found${NC}"
    fi

elif [ "$1" = "view" ]; then
    if [ -z "$2" ]; then
        echo -e "${RED}Error: Please specify a state to view${NC}"
        echo "Usage: $0 view <state>"
        echo "Example: $0 view california"
        exit 1
    fi

    state="$2"
    checkpoint_file="$CHECKPOINT_DIR/${state}.json"

    if [ -f "$checkpoint_file" ]; then
        echo -e "${GREEN}📍 Checkpoint for ${state}:${NC}"
        echo ""
        if command -v jq &> /dev/null; then
            jq '.' "$checkpoint_file"
        else
            cat "$checkpoint_file"
        fi
    else
        echo -e "${YELLOW}No checkpoint found for ${state}${NC}"
    fi

elif [ "$1" = "help" ] || [ "$1" = "-h" ] || [ "$1" = "--help" ]; then
    echo "Usage: $0 <command> [options]"
    echo ""
    echo "Commands:"
    echo "  list              List all checkpoints (default)"
    echo "  view <state>      View checkpoint details for a specific state"
    echo "  clear <state>     Clear checkpoint for a specific state"
    echo "  clear-all         Clear all checkpoints"
    echo "  help              Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 list"
    echo "  $0 view california"
    echo "  $0 clear california"
    echo "  $0 clear-all"
    echo ""

else
    echo -e "${RED}Unknown command: $1${NC}"
    echo "Run '$0 help' for usage information"
    exit 1
fi
