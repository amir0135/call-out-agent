#!/usr/bin/env bash
# ------------------------------------------------------------------
# demo.sh — one-command startup for the Call-Out Agent demo.
#
# What it does:
#   1. Frees port 8000 if another container is holding it.
#   2. Cleans up any stale call-out containers.
#   3. (Re)hosts a devtunnel on port 8000 in the background.
#   4. Updates CALLBACK_BASE_URL in .env to the current tunnel URL.
#   5. Builds and starts the docker compose stack.
#   6. Waits for /health on localhost AND the tunnel to return 200.
#   7. Exports TUNNEL_URL and prints the ready-to-paste test_call.sh command.
#
# Usage:
#   ./scripts/demo.sh                 # start everything
#   ./scripts/demo.sh stop            # stop everything
#   ./scripts/demo.sh tunnel          # only (re)host the tunnel
#   ./scripts/demo.sh status          # print current state + URLs
# ------------------------------------------------------------------
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
cd "$PROJECT_DIR"

TUNNEL_LOG="/tmp/callout-devtunnel.log"
TUNNEL_URL_FILE="/tmp/callout-tunnel-url"
TUNNEL_PID_FILE="/tmp/callout-devtunnel.pid"
PORT=8000
PHONE_DEFAULT="+4512345678"

RED='\033[0;31m'
GRN='\033[0;32m'
YEL='\033[1;33m'
CYA='\033[0;36m'
NC='\033[0m'

say()  { echo -e "${CYA}==>${NC} $*"; }
ok()   { echo -e "${GRN}  ok${NC} $*"; }
warn() { echo -e "${YEL}  !!${NC} $*"; }
err()  { echo -e "${RED}  xx${NC} $*" >&2; }

# ------------------------------------------------------------------
ensure_docker() {
    if docker info >/dev/null 2>&1; then
        return 0
    fi
    say "Docker daemon not reachable \u2014 starting Docker Desktop"
    if [ "$(uname)" = "Darwin" ] && [ -d "/Applications/Docker.app" ]; then
        open -g -a Docker
    elif command -v systemctl >/dev/null 2>&1; then
        sudo systemctl start docker >/dev/null 2>&1 || true
    else
        err "Please start Docker Desktop manually, then rerun $0"
        exit 1
    fi

    printf "    waiting for daemon"
    local tries=0
    while [ $tries -lt 120 ]; do
        if docker info >/dev/null 2>&1; then
            echo ""
            ok "Docker daemon ready"
            return 0
        fi
        printf "."
        sleep 1
        tries=$((tries + 1))
    done
    echo ""
    err "Docker daemon did not come up after 2 minutes. Start Docker Desktop manually and retry."
    exit 1
}
free_port() {
    local pid_container
    pid_container=$(docker ps --filter "publish=${PORT}" --format '{{.Names}}' | grep -v 'call-out-agent-orchestrator' || true)
    if [ -n "$pid_container" ]; then
        warn "port $PORT held by: $pid_container — stopping it"
        docker stop $pid_container >/dev/null
        ok "freed port $PORT"
    fi

    # Also kill any host process on the port (non-docker)
    if command -v lsof >/dev/null 2>&1; then
        local host_pids
        host_pids=$(lsof -ti tcp:$PORT 2>/dev/null || true)
        if [ -n "$host_pids" ]; then
            warn "host process on :$PORT (pids: $host_pids) — killing"
            kill -9 $host_pids 2>/dev/null || true
        fi
    fi
}

clean_stack() {
    say "cleaning any previous call-out containers"
    docker compose down --remove-orphans >/dev/null 2>&1 || true
    ok "stack down"
}

host_tunnel() {
    say "checking devtunnel CLI"
    if ! command -v devtunnel >/dev/null 2>&1; then
        err "devtunnel not installed. Install from https://learn.microsoft.com/azure/developer/dev-tunnels/get-started"
        exit 1
    fi

    # Reuse an existing running host if it's alive and still has a port-qualified URL.
    if [ -f "$TUNNEL_PID_FILE" ] && [ -f "$TUNNEL_URL_FILE" ]; then
        local old_pid old_url
        old_pid=$(cat "$TUNNEL_PID_FILE")
        old_url=$(cat "$TUNNEL_URL_FILE")
        if kill -0 "$old_pid" 2>/dev/null \
           && [ -n "$old_url" ] \
           && [[ "$old_url" == *"-${PORT}."* ]]; then
            ok "reusing existing tunnel (pid $old_pid): $old_url"
            return 0
        fi
    fi

    # Also reuse any other devtunnel host process already running (e.g. user started one manually).
    local running_pid
    running_pid=$(pgrep -f 'devtunnel host' | head -1 || true)
    if [ -n "$running_pid" ]; then
        local url=""
        if [ -f "$TUNNEL_LOG" ]; then
            url=$(grep -Eo "https://[a-z0-9]+-${PORT}\\.[a-z0-9.-]+\\.devtunnels\\.ms" "$TUNNEL_LOG" 2>/dev/null \
                  | grep -v -- '-inspect' | head -1 || true)
        fi
        if [ -n "$url" ]; then
            echo "$running_pid" > "$TUNNEL_PID_FILE"
            echo "$url" > "$TUNNEL_URL_FILE"
            ok "reusing existing devtunnel host (pid $running_pid): $url"
            return 0
        fi
        warn "a devtunnel host (pid $running_pid) is running but port-$PORT URL not found; killing it"
        kill "$running_pid" 2>/dev/null || true
        sleep 1
    fi

    say "starting devtunnel on :$PORT (log: $TUNNEL_LOG)"
    : > "$TUNNEL_LOG"
    # shellcheck disable=SC2024
    nohup devtunnel host -p $PORT --allow-anonymous >"$TUNNEL_LOG" 2>&1 &
    echo $! > "$TUNNEL_PID_FILE"

    # Wait up to 40s for the port-qualified URL to appear in the log.
    # Example match: https://lpmfvmrm-8000.eun1.devtunnels.ms
    local url="" tries=0
    printf "    detecting URL"
    while [ $tries -lt 80 ]; do
        url=$(grep -Eo "https://[a-z0-9]+-${PORT}\\.[a-z0-9.-]+\\.devtunnels\\.ms" "$TUNNEL_LOG" 2>/dev/null \
              | grep -v -- '-inspect' \
              | head -1 || true)
        if [ -n "$url" ]; then
            break
        fi
        printf "."
        sleep 0.5
        tries=$((tries + 1))
    done
    echo ""

    if [ -z "$url" ]; then
        err "could not detect tunnel URL after 40s. Log tail:"
        tail -20 "$TUNNEL_LOG"
        echo ""
        warn "try manually: devtunnel host -p $PORT --allow-anonymous"
        exit 1
    fi

    echo "$url" > "$TUNNEL_URL_FILE"
    ok "tunnel: $url"
}

sync_env() {
    local url
    url=$(cat "$TUNNEL_URL_FILE")

    if [ ! -f .env ]; then
        err ".env does not exist. Copy .env.example and fill in ACS_ENDPOINT + ACS_PHONE_NUMBER first."
        exit 1
    fi

    say "updating CALLBACK_BASE_URL in .env"
    if grep -q '^CALLBACK_BASE_URL=' .env; then
        # portable in-place edit (macOS + linux)
        python3 - "$url" <<'PY'
import re, sys, pathlib
url = sys.argv[1]
p = pathlib.Path(".env")
text = p.read_text()
text = re.sub(r"^CALLBACK_BASE_URL=.*$", f"CALLBACK_BASE_URL={url}", text, flags=re.M)
p.write_text(text)
PY
    else
        echo "CALLBACK_BASE_URL=$url" >> .env
    fi
    ok "CALLBACK_BASE_URL=$url"
}

start_stack() {
    say "building + starting docker compose (detached)"
    docker compose up --build -d
    ok "stack up"
}

wait_healthy() {
    local url=$1 label=$2 max=${3:-60} tries=0
    say "waiting for $label -> $url/health (up to ${max}s)"
    while [ $tries -lt $max ]; do
        if curl -sf --max-time 5 "$url/health" >/dev/null 2>&1; then
            ok "$label healthy"
            return 0
        fi
        sleep 1
        tries=$((tries + 1))
    done
    warn "$label did not respond within ${max}s (may still come up shortly)"
    return 1
}

print_summary() {
    local url
    url=$(cat "$TUNNEL_URL_FILE")
    echo ""
    echo -e "${GRN}===============================================${NC}"
    echo -e "${GRN}  Demo is ready${NC}"
    echo -e "${GRN}===============================================${NC}"
    echo ""
    echo "  Local:   http://localhost:$PORT"
    echo "  Tunnel:  $url"
    echo ""
    echo "  Trigger a test call:"
    echo -e "    ${CYA}export TUNNEL_URL=\"$url\"${NC}"
    echo -e "    ${CYA}./scripts/test_call.sh $PHONE_DEFAULT \"\$TUNNEL_URL\"${NC}"
    echo ""
    echo "  (or, equivalently, read the URL from the state file:)"
    echo -e "    ${CYA}export TUNNEL_URL=\"\$(cat /tmp/callout-tunnel-url)\"${NC}"
    echo ""
    echo "  Tail orchestrator logs (AUDIT | ... lines):"
    echo -e "    ${CYA}docker compose logs -f orchestrator | grep --line-buffered AUDIT${NC}"
    echo ""
    echo "  Stop everything:"
    echo -e "    ${CYA}./scripts/demo.sh stop${NC}"
    echo ""
}

cmd_start() {
    ensure_docker
    free_port
    clean_stack
    host_tunnel
    sync_env
    start_stack
    # Local must be healthy — fail if not
    if ! wait_healthy "http://localhost:$PORT" "local" 60; then
        err "orchestrator did not start. Logs:"
        docker compose logs --tail 60 orchestrator
        exit 1
    fi
    # Tunnel is best-effort — anonymous devtunnels need a warmup + consent hit
    local url
    url=$(cat "$TUNNEL_URL_FILE")
    wait_healthy "$url" "tunnel" 120 || \
        warn "open $url/health in a browser once to warm it up (anonymous tunnels may need a first visit), then retry ./scripts/demo.sh status"
    print_summary
}

cmd_stop() {
    say "stopping stack"
    docker compose down --remove-orphans || true

    if [ -f "$TUNNEL_PID_FILE" ]; then
        local pid
        pid=$(cat "$TUNNEL_PID_FILE")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null || true
            ok "stopped tunnel (pid $pid)"
        fi
        rm -f "$TUNNEL_PID_FILE"
    fi
    ok "stopped"
}

cmd_status() {
    echo "Containers:"
    docker compose ps || true
    echo ""
    if [ -f "$TUNNEL_URL_FILE" ]; then
        echo "Tunnel URL: $(cat "$TUNNEL_URL_FILE")"
    else
        echo "Tunnel URL: (none)"
    fi
    echo ""
    echo -n "Local  /health: "; curl -sf --max-time 2 "http://localhost:$PORT/health" && echo || echo "DOWN"
    if [ -f "$TUNNEL_URL_FILE" ]; then
        echo -n "Tunnel /health: "; curl -sf --max-time 5 "$(cat "$TUNNEL_URL_FILE")/health" && echo || echo "DOWN"
    fi
}

cmd_tunnel() {
    host_tunnel
    sync_env
    ok "tunnel ready: $(cat "$TUNNEL_URL_FILE")"
    warn "remember to restart the stack so the orchestrator picks up the new CALLBACK_BASE_URL:"
    echo "    docker compose up -d --force-recreate orchestrator"
}

case "${1:-start}" in
    start)  cmd_start ;;
    stop)   cmd_stop ;;
    status) cmd_status ;;
    tunnel) cmd_tunnel ;;
    *)      err "unknown command: $1"; echo "Usage: $0 [start|stop|status|tunnel]"; exit 1 ;;
esac
