#!/bin/bash
DIR="$(cd "$(dirname "$0")" && pwd)"
PIDFILE="$DIR/.server.pid"

if [ -f "$PIDFILE" ]; then
    OLD_PID=$(cat "$PIDFILE")
    if kill -0 "$OLD_PID" 2>/dev/null; then
        echo "Server is already running (PID: $OLD_PID)"
        exit 1
    fi
    rm -f "$PIDFILE"
fi

PYTHONWARNINGS="ignore" nohup python "$DIR/server.py" > "$DIR/server.log" 2>&1 &
PID=$!
echo $PID > "$PIDFILE"
echo "Server started (PID: $PID)"
echo "Logs: $DIR/server.log"
