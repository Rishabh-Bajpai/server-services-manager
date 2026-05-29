#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.server.pid"

# Check if running as systemd user service first
if systemctl --user is-active server-services-manager &>/dev/null; then
    echo "Stopping systemd service..."
    systemctl --user stop server-services-manager
    echo "Server stopped."
    exit 0
fi

if [ -f "$PIDFILE" ]; then
    PID=$(cat "$PIDFILE")
    echo "Stopping server (PID: $PID)..."
    kill "$PID" 2>/dev/null
    for i in $(seq 1 10); do
        if ! kill -0 "$PID" 2>/dev/null; then
            break
        fi
        sleep 0.5
    done
    kill -9 "$PID" 2>/dev/null
    rm -f "$PIDFILE"
    echo "Server stopped."
else
    PID=$(pgrep -f "python.*server\.py" 2>/dev/null | head -1)
    if [ -n "$PID" ]; then
        echo "Stopping server (PID: $PID)..."
        kill "$PID" 2>/dev/null
        rm -f "$PIDFILE"
        echo "Server stopped."
    else
        echo "No server running."
    fi
fi
