#!/usr/bin/env bash
# Neko trading bot stack — auto-start + keep-alive.
# Run: nohup bash start.sh > /tmp/neko-start.log 2>&1 &
# Stop: pkill -f start.sh

REPO="$(cd "$(dirname "$0")" && pwd)"
cd "$REPO"
source .venv/bin/activate

# Kill any stale processes first
pkill -f "uvicorn main:app" 2>/dev/null || true
pkill -f "worker.py" 2>/dev/null || true
pkill -f "tg_bot/main.py" 2>/dev/null || true
sleep 2

start_service() {
    local name="$1" cmd="$2"
    # Each service runs in its OWN background subshell so the loop never
    # blocks the others from starting.
    (
        while true; do
            echo "[$(date '+%H:%M:%S')] starting $name..."
            eval "$cmd" &
            local pid=$!
            echo "[$(date '+%H:%M:%S')] $name pid=$pid"
            # wait returns the child's exit code (non-zero = crash). Disable -e
            # so a crash doesn't kill the keep-alive loop; we sleep and restart.
            set +e
            wait "$pid"
            set -e
            echo "[$(date '+%H:%M:%S')] $name exited (pid=$pid) — restarting in 5s"
            sleep 5
        done
    ) &
}

# Launch all three keep-alive loops in parallel (each is a background subshell).
start_service "uvicorn" \
    ".venv/bin/python -m uvicorn main:app --app-dir service/server --host 127.0.0.1 --port 8000 > /tmp/neko-server.log 2>&1"

start_service "worker" \
    ".venv/bin/python service/server/worker.py > /tmp/neko-worker.log 2>&1"

start_service "tg_bot" \
    ".venv/bin/python -u service/tg_bot/main.py > /tmp/neko-tgbot.log 2>&1"

# Keep the parent alive so `pkill -f start.sh` can stop everything.
wait