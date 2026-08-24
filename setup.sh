#!/usr/bin/env bash
# ------------------------------------------------------------------
# setup.sh — one-command setup for the Call-Out Agent.
#
#   git clone https://github.com/amir0135/call-out-agent.git
#   cd call-out-agent
#   ./setup.sh
#
# What it does:
#   1. Checks prerequisites (python3 required; az / azd / docker / devtunnel
#      are optional and only needed to deploy or place live calls).
#   2. Creates a Python virtual environment (.venv) and installs all
#      dependencies, including the test tools.
#   3. Creates .env from .env.example if it does not exist yet.
#   4. Runs the test suite to verify the checkout is healthy.
#   5. Prints clear next steps (run locally, or deploy to Azure).
#
# Re-runnable: safe to run repeatedly. Set SKIP_TESTS=1 to skip step 4.
# ------------------------------------------------------------------
set -euo pipefail

cd "$(dirname "$0")"

BOLD='\033[1m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; CYAN='\033[0;36m'; NC='\033[0m'
say()  { echo -e "${CYAN}==>${NC} $*"; }
ok()   { echo -e "  ${GREEN}ok${NC} $*"; }
warn() { echo -e "  ${YELLOW}!!${NC} $*"; }
err()  { echo -e "  ${RED}xx${NC} $*" >&2; }

MIN_PY_MINOR=10   # requires Python 3.10+

case "$(uname -s)" in
    Darwin) OS="macOS";  INSTALL_HINT="brew install" ;;
    Linux)  OS="Linux";  INSTALL_HINT="your package manager (apt/dnf/…) " ;;
    *)      OS="$(uname -s)"; INSTALL_HINT="your package manager" ;;
esac

echo ""
echo -e "${BOLD}=====================================================${NC}"
echo -e "${BOLD}  Call-Out Agent — setup  (${OS})${NC}"
echo -e "${BOLD}=====================================================${NC}"

# ------------------------------------------------------------------
# 1. Prerequisites
# ------------------------------------------------------------------
say "Checking prerequisites"

PY=""
for c in python3.12 python3.11 python3.10 python3; do
    if command -v "$c" >/dev/null 2>&1; then PY="$c"; break; fi
done
if [ -z "$PY" ]; then
    err "python3 not found. Install Python 3.10+ with ${INSTALL_HINT} python3, then re-run."
    exit 1
fi
PY_MINOR="$("$PY" -c 'import sys; print(sys.version_info.minor)')"
PY_MAJOR="$("$PY" -c 'import sys; print(sys.version_info.major)')"
if [ "$PY_MAJOR" -ne 3 ] || [ "$PY_MINOR" -lt "$MIN_PY_MINOR" ]; then
    err "Python 3.${MIN_PY_MINOR}+ required; found $("$PY" --version 2>&1). Install a newer Python and re-run."
    exit 1
fi
ok "$("$PY" --version 2>&1) ($PY)"

# Optional tooling — needed to deploy or place live calls, not for tests.
check_optional() {
    local cmd="$1" purpose="$2"
    if command -v "$cmd" >/dev/null 2>&1; then
        ok "$cmd found — $purpose"
    else
        warn "$cmd not found — needed to $purpose. Install later with: ${INSTALL_HINT} $cmd"
    fi
}
check_optional az       "manage Azure resources"
check_optional azd      "deploy the whole stack (azd up)"
check_optional docker   "run the stack locally (docker compose)"
check_optional devtunnel "expose port 8000 so ACS can reach you for local live calls"

# ------------------------------------------------------------------
# 2. Virtual environment + dependencies
# ------------------------------------------------------------------
say "Creating the Python virtual environment (.venv)"
if [ ! -d .venv ]; then
    "$PY" -m venv .venv
    ok "created .venv"
else
    ok ".venv already exists — reusing"
fi
VENV_PY=".venv/bin/python"

say "Installing dependencies (this can take a minute)"
"$VENV_PY" -m pip install --quiet --upgrade pip
"$VENV_PY" -m pip install --quiet -r src/orchestrator/requirements.txt
"$VENV_PY" -m pip install --quiet -r src/agent/requirements.txt
"$VENV_PY" -m pip install --quiet pytest pytest-asyncio pytest-httpx
ok "dependencies installed into .venv"

# ------------------------------------------------------------------
# 3. .env
# ------------------------------------------------------------------
say "Preparing .env"
if [ -f .env ]; then
    ok ".env already exists — leaving it untouched"
elif [ -f .env.example ]; then
    cp .env.example .env
    ok "created .env from .env.example"
    warn "Edit .env and fill in your own values before placing live calls (see comments in the file)."
else
    warn ".env.example not found — skipping .env creation"
fi

# ------------------------------------------------------------------
# 4. Verify with the test suite
# ------------------------------------------------------------------
if [ "${SKIP_TESTS:-0}" = "1" ]; then
    warn "SKIP_TESTS=1 set — skipping the test suite"
else
    say "Running the test suite to verify the checkout"
    if "$VENV_PY" -m pytest -q; then
        ok "all tests passed"
    else
        err "tests failed — the code is checked out but something is off. See the output above."
        exit 1
    fi
fi

# ------------------------------------------------------------------
# 5. Next steps
# ------------------------------------------------------------------
echo ""
echo -e "${BOLD}=====================================================${NC}"
echo -e "${GREEN}${BOLD}  Setup complete.${NC}"
echo -e "${BOLD}=====================================================${NC}"
cat <<'NEXT'

Next steps:

  • Run the tests again anytime:
        .venv/bin/python -m pytest

  • Run the whole stack locally (needs Docker + a devtunnel for live calls):
        docker compose up --build
        # in another terminal, expose port 8000 so ACS can call back:
        devtunnel host --port-numbers 8000 --allow-anonymous
        # put the tunnel URL into .env as CALLBACK_BASE_URL, then trigger a call:
        ./scripts/test_call.sh +YOURPHONE "$CALLBACK_BASE_URL"

  • Deploy the entire stack to Azure (provisions Container Apps, Cosmos, OpenAI,
    Speech, AI Search, ACR, App Insights via infra/ Bicep):
        azd auth login
        azd up

  See README.md for the Azure prerequisites (ACS phone number, Speech resource)
  and docs/DEMO-WALKTHROUGH.md for a guided tour.

NEXT
