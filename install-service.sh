#!/bin/bash
set -e

DIR="$(cd "$(dirname "$0")" && pwd)"
SERVICE_NAME="server-services-manager"
SERVICE_FILE="$HOME/.config/systemd/user/${SERVICE_NAME}.service"

# Find Python
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

echo "Using Python: $PYTHON"

# Validate .env exists
if [ -f "$DIR/.env" ]; then
    echo "Using .env for configuration"
else
    echo "Warning: No .env file found at $DIR/.env"
    echo "Creating one with default password..."
    echo "PASSWORD=admin" > "$DIR/.env"
    echo "Set a custom password: echo \"PASSWORD=your_password\" > \"$DIR/.env\""
fi

# Create user systemd directory
mkdir -p "$HOME/.config/systemd/user"

# Generate service file from template
sed \
    -e "s|%%PROJECT_DIR%%|$DIR|g" \
    -e "s|%%PYTHON_PATH%%|$PYTHON|g" \
    "$DIR/$SERVICE_NAME.service" > "$SERVICE_FILE"

echo "Service file created: $SERVICE_FILE"

# Enable linger so user services start on boot
if command -v loginctl &>/dev/null; then
    USER=$(whoami)
    LINGER=$(loginctl show-user "$USER" 2>/dev/null | grep "^Linger=" | cut -d= -f2)
    if [ "$LINGER" != "yes" ]; then
        echo "Enabling linger for $USER..."
        loginctl enable-linger "$USER" 2>/dev/null || echo "Warning: could not enable linger (try: sudo loginctl enable-linger $USER)"
    fi
fi

# Reload, enable, and start
systemctl --user daemon-reload
systemctl --user enable "$SERVICE_NAME"
systemctl --user restart "$SERVICE_NAME"

echo ""
echo "Service installed and started!"
echo "  Status:  systemctl --user status $SERVICE_NAME"
echo "  Logs:    journalctl --user -u $SERVICE_NAME -f"
echo "  URL:     http://localhost:8881"
