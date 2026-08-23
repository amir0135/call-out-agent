#!/usr/bin/env bash
set -euo pipefail

# ------------------------------------------------------------------
# Trigger a test alarm call
# Usage: ./scripts/test_call.sh [phone_number] [base_url]
#
# If no phone number is given on the command line, falls back to
# $TEST_PHONE_NUMBER (loaded from .env when present). This keeps
# day-to-day testing as simple as `./scripts/test_call.sh`.
# ------------------------------------------------------------------

# Load .env if present so TEST_PHONE_NUMBER is available without
# requiring the caller to source it manually.
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
if [ -f "$PROJECT_DIR/.env" ]; then
  set -a
  # shellcheck disable=SC1091
  source "$PROJECT_DIR/.env"
  set +a
fi

PHONE="${1:-${TEST_PHONE_NUMBER:-}}"
if [ -z "$PHONE" ]; then
  echo "Usage: ./scripts/test_call.sh <phone_number> [base_url]"
  echo "(or set TEST_PHONE_NUMBER in .env)"
  exit 1
fi
BASE_URL="${2:-http://localhost:8000}"

echo "Triggering test call to $PHONE via $BASE_URL ..."
echo ""

# Fail fast if the orchestrator isn't reachable
if ! curl -sf -o /dev/null --connect-timeout 3 "$BASE_URL/health"; then
  echo "ERROR: Orchestrator not reachable at $BASE_URL"
  echo "Start it first with one of:"
  echo "  ./scripts/dev_local.sh        # local (needs .env + devtunnel)"
  echo "  docker compose up --build     # containers"
  exit 1
fi

RESPONSE=$(curl -s -w "\n__HTTP_STATUS__:%{http_code}" -X POST "$BASE_URL/api/alarm-intent" \
  -H "Content-Type: application/json" \
  -d "{
    \"alarm\": {
      \"alarm_id\": \"ALM-TEST-$(date +%s)\",
      \"alarm_type\": \"high_temperature\",
      \"severity\": \"high\",
      \"store_name\": \"Copenhagen Central Market\",
      \"store_id\": \"ST-CPH-01\",
      \"equipment_name\": \"Walk-in Cooler #3\",
      \"current_temp\": 14.2,
      \"threshold_temp\": 8.0,
      \"alarm_time\": \"$(date -u +%Y-%m-%dT%H:%M:%SZ)\",
      \"customer_id\": \"CUST-CCC-001\"
    },
    \"phone_number\": \"$PHONE\"
  }")

BODY="${RESPONSE%$'\n'__HTTP_STATUS__:*}"
STATUS="${RESPONSE##*__HTTP_STATUS__:}"

echo "HTTP $STATUS"
if [ -z "$BODY" ]; then
  echo "(empty response body)"
elif echo "$BODY" | python3 -m json.tool 2>/dev/null; then
  :
else
  echo "$BODY"
fi

echo ""
echo "Check the orchestrator logs:  docker compose logs -f orchestrator"
