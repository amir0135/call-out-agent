#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Local development startup script
# Starts the agent + orchestrator with live ACS calling enabled.
# ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

# Colours
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

echo ""
echo "==========================================="
echo "  Contoso Call-Out Agent — Local Dev Setup"
echo "==========================================="
echo ""

# --- Check .env ---
if [ ! -f .env ]; then
    echo -e "${YELLOW}No .env file found. Creating from .env.example...${NC}"
    cp .env.example .env
    echo -e "${RED}⚠  Edit .env and fill in your ACS_ENDPOINT, ACS_PHONE_NUMBER, and CALLBACK_BASE_URL${NC}"
    echo "   Then run this script again."
    echo ""
    echo "   Quick setup:"
    echo "   1. Create ACS resource:  az communication create -n <name> -g <rg> --data-location US"
    echo "   2. Get a phone number:   Azure Portal → ACS → Phone Numbers → Get"
    echo "   3. Start a tunnel:       devtunnel host --port-numbers 8000 --allow-anonymous"
    echo "   4. Paste the tunnel URL into .env as CALLBACK_BASE_URL"
    echo ""
    exit 1
fi

# --- Source .env ---
set -a
source .env
set +a

# --- Validate required vars ---
MISSING=0
for VAR in ACS_ENDPOINT ACS_PHONE_NUMBER CALLBACK_BASE_URL; do
    VAL="${!VAR:-}"
    if [ -z "$VAL" ] || [[ "$VAL" == *"<"* ]]; then
        echo -e "${RED}✗ $VAR is not set in .env${NC}"
        MISSING=1
    else
        echo -e "${GREEN}✓ $VAR${NC} = ${VAL:0:40}..."
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo ""
    echo -e "${RED}Fix the values in .env and try again.${NC}"
    exit 1
fi

# --- Check az login ---
if ! az account show &>/dev/null; then
    echo ""
    echo -e "${YELLOW}Not logged in to Azure CLI. Running 'az login'...${NC}"
    az login
fi
echo -e "${GREEN}✓ Azure CLI${NC} logged in as $(az account show --query user.name -o tsv)"

# --- Check Docker ---
if ! docker info &>/dev/null; then
    echo ""
    echo -e "${RED}✗ Docker is not running. Start Docker Desktop and try again.${NC}"
    exit 1
fi
echo -e "${GREEN}✓ Docker${NC} is running"

echo ""
echo "Starting services..."
echo ""

# --- Build and start ---
docker compose up --build
