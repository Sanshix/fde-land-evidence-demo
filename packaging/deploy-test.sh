#!/bin/sh
# Isolated deployment acceptance: loopback only, no system installation.
set -eu
cd "$(dirname "$0")"
ROOT=$(pwd -P)
export LC_ALL=C
export FDE_HOST=127.0.0.1
export FDE_PORT=18765
export FDE_DATA_DIR="$ROOT/data"
export FDE_SAMPLES_DIR="$ROOT/练习集"
# Deployment acceptance does not call cloud models.
export FDE_DISABLE_WORKER=1
export FDE_DEMO_ROOT="$ROOT/data/test-demo"
mkdir -p logs
PIDFILE="$ROOT/logs/server.pid"

owned_pid() {
    [ -f "$PIDFILE" ] || return 1
    PID=$(cat "$PIDFILE")
    case "$PID" in ''|*[!0-9]*) return 1 ;; esac
    EXE=$(readlink "/proc/$PID/exe" 2>/dev/null || true)
    [ "$EXE" = "$ROOT/fde" ] || [ "$EXE" = "$ROOT/runtime/bin/python3.12" ]
}

stop() {
    if owned_pid; then
        kill -TERM "$PID"
        n=0
        while owned_pid; do
            n=$((n + 1))
            [ "$n" -lt 30 ] || { echo 'Stop timed out; inspect logs/server.log'; exit 1; }
            sleep 1
        done
        echo 'Stopped.'
    else
        echo 'Already stopped.'
    fi
}

case "${1:-start}" in
    stop) stop; exit 0 ;;
    status)
        if owned_pid; then
            curl --fail --silent --show-error --max-time 5 http://127.0.0.1:18765/healthz
            echo
            exit 0
        fi
        echo 'Stopped.'; exit 1 ;;
    start) ;;
    *) echo 'Usage: sh deploy-test.sh [start|stop|status]'; exit 2 ;;
esac

if owned_pid; then
    echo 'Already running.'
    exit 0
fi
if ss -ltn | awk '$4 ~ /:18765$/ {found=1} END {exit !found}'; then
    echo 'Port 18765 is occupied; no process was stopped.' >&2
    exit 1
fi
sha256sum -c CHECKSUMS.sha256 > logs/checksums.log
chmod u+x ./fde
./fde --install >> logs/install.log 2>&1
nohup ./fde --no-browser >> logs/server.log 2>&1 < /dev/null &
PID=$!
printf '%s\n' "$PID" > "$PIDFILE"
n=0
while ! curl --fail --silent --max-time 2 http://127.0.0.1:18765/healthz > logs/health.json; do
    if ! kill -0 "$PID" 2>/dev/null; then
        echo 'Startup failed; inspect logs/server.log' >&2
        exit 1
    fi
    n=$((n + 1))
    if [ "$n" -ge 60 ]; then
        stop
        echo 'Health check timed out; inspect logs/server.log' >&2
        exit 1
    fi
    sleep 1
done
echo 'Ready: http://127.0.0.1:18765 (cloud workers disabled for deployment acceptance)'
cat logs/health.json
echo
