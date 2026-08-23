#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Run both services locally WITHOUT Docker.
# The agent runs in local mock mode (rule-based, no Azure OpenAI).
# The orchestrator uses real ACS for outbound calls.
#
# Prerequisites:
#   1. ACS resource with a phone number
#   2. Public tunnel for ACS callbacks (devtunnel or ngrok)
#   3. az login
#
# Usage:
#   cp .env.example .env   # fill in ACS values
#   ./scripts/dev_local.sh
# ------------------------------------------------------------------

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BOLD='\033[1m'
NC='\033[0m'

echo ""
echo -e "${BOLD}==========================================${NC}"
echo -e "${BOLD}  Contoso Call-Out Agent — Local Dev${NC}"
echo -e "${BOLD}==========================================${NC}"
echo ""

# --- Check .env ---
if [ ! -f .env ]; then
    cp .env.example .env
    echo -e "${RED}Created .env from template. Edit it with your ACS values:${NC}"
    echo "   ACS_ENDPOINT=https://<your-acs>.communication.azure.com"
    echo "   ACS_PHONE_NUMBER=+1XXXXXXXXXX"
    echo "   CALLBACK_BASE_URL=https://<your-tunnel-url>"
    echo ""
    exit 1
fi

set -a
source .env
set +a

# Override for local mode
export LOCAL_MODE=true
export FOUNDRY_AGENT_ENDPOINT=http://localhost:8080

# --- Validate ---
MISSING=0
for VAR in ACS_ENDPOINT ACS_PHONE_NUMBER CALLBACK_BASE_URL; do
    VAL="${!VAR:-}"
    if [ -z "$VAL" ] || [[ "$VAL" == *"<"* ]]; then
        echo -e "${RED}✗ $VAR not set${NC}"
        MISSING=1
    else
        echo -e "${GREEN}✓ $VAR${NC}"
    fi
done

if [ "$MISSING" -eq 1 ]; then
    echo -e "\n${RED}Fix .env and retry.${NC}"
    exit 1
fi

echo -e "${GREEN}✓ LOCAL_MODE${NC} = true (rule-based agent, no Azure OpenAI)"
echo ""

# --- Install deps if needed ---
if [ ! -d .venv ]; then
    echo "Creating virtual environment..."
    python3 -m venv .venv
fi
source .venv/bin/activate

echo "Installing dependencies..."
pip install -q \
    fastapi uvicorn pydantic httpx \
    azure-communication-callautomation azure-identity azure-cosmos \
    openai 2>&1 | tail -1

echo ""

# --- Start agent in background ---
echo -e "${BOLD}Starting agent on :8080...${NC}"
PYTHONPATH="$PROJECT_DIR/src" uvicorn agent.agent:app \
    --host 0.0.0.0 --port 8080 --log-level info &
AGENT_PID=$!

# Wait for agent
for i in {1..10}; do
    if curl -sf http://localhost:8080/health >/dev/null 2>&1; then
        echo -e "${GREEN}✓ Agent is up${NC}"
        break
    fi
    sleep 0.5
done

# --- Start orchestrator in foreground ---
echo -e "${BOLD}Starting orchestrator on :8000...${NC}"
echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo -e "${YELLOW}  Ready! Trigger a call:${NC}"
echo -e "${YELLOW}  ./scripts/test_call.sh +<phone_number>${NC}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""

cleanup() {
    echo ""
    echo "Shutting down..."
    kill $AGENT_PID 2>/dev/null || true
    wait $AGENT_PID 2>/dev/null || true
    echo "Done."
}
trap cleanup EXIT INT TERM

PYTHONPATH="$PROJECT_DIR/src" uvicorn orchestrator.app:app \
    --host 0.0.0.0 --port 8000 --log-level info
