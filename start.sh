#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.server.pid"

# Check if systemd user service is running
if systemctl --user is-active server-services-manager &>/dev/null; then
    echo "Server is already running as a systemd user service."
    echo "  Logs: journalctl --user -u server-services-manager -f"
    exit 0
fi

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Server is already running (PID: $OLD_PID)"
        exit 1
    fi
    rm -f "$PIDFILE"
fi

PYTHON=""
for p in "$DIR/venv/bin/python3" "$DIR/venv/bin/python" "$(which python3)" "$(which python)"; do
    if [ -x "$p" ]; then
        PYTHON="$p"
        break
    fi
done

if [ -z "$PYTHON" ]; then
    echo "Error: Python not found"
    exit 1
fi

PYTHONWARNINGS="ignore" nohup "$PYTHON" "$DIR/server.py" > "$DIR/server.log" 2>&1 &
PID=$!
echo $PID > "$PIDFILE"
echo "Server started (PID: $PID)"
echo "Logs: $DIR/server.log"
echo ""
echo "Tip: Install as a systemd service for persistent operation:"
echo "  bash $DIR/install-service.sh"
