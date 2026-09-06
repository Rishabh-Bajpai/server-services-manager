#!/usr/bin/env bash
# install-code-server.sh — user-level VS Code Server for the /code page.
#
# Idempotent: safe to re-run. Does NOT need root (everything lives
# under $HOME). Mirrors install-service.sh precedent.
#
#   1. Installs the code-server standalone tarball into
#      ~/.local/lib + ~/.local/bin (pinned CODE_VERSION, no sudo).
#   2. Writes ~/.config/code-server/config.yaml bound to
#      0.0.0.0:8600 with password auth, syncing the password
#      from the app's .env PASSWORD (same login the user knows).
#      0.0.0.0 (not loopback) so LAN/Tailscale browsers can reach
#      it; password auth stays on. A non-default existing
#      bind-addr is preserved; the password is synced on re-runs.
#   3. Installs the Office Viewer extension (cweijan.vscode-office)
#      inside code-server for .docx/.xlsx/.pptx, unless present.
#   4. Registers a `code-server` managed program in config.yaml
#      (autostart) unless one already exists.
#
# Afterward: restart the app (./stop.sh && ./start.sh) and open /code.
set -euo pipefail

CODE_VERSION="${CODE_VERSION:-4.135.0}"
# An explicitly exported CODE_PORT always wins. Otherwise an existing
# bind-addr is preserved — unless it is a localhost default (the
# factory 127.0.0.1:8080 or our old 127.0.0.1:8600). Loopback-only
# breaks remote access (e.g. over Tailscale): the browser connects
# to this box's LAN/tailscale address, where nothing would listen.
# 0.0.0.0 keeps password auth on, so the gate is identical to the
# app itself — scope it further with the firewall if needed
# (e.g. allow 8600 only from 100.64.0.0/10 for Tailscale-only).
if [ -z "${CODE_PORT+x}" ]; then CODE_PORT="8600"; CODE_PORT_EXPLICIT=0; else CODE_PORT_EXPLICIT=1; fi
if [ -z "${BIND_HOST+x}" ]; then BIND_HOST="0.0.0.0"; BIND_HOST_EXPLICIT=0; else BIND_HOST_EXPLICIT=1; fi
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
BIN="$HOME/.local/bin/code-server"

echo "==> code-server ${CODE_VERSION} (port ${CODE_PORT})"

# --- 1. binary -------------------------------------------------------------
if [ -x "$BIN" ]; then
    echo "binary already present: $BIN"
else
    echo "downloading standalone tarball…"
    mkdir -p "$HOME/.local/lib" "$HOME/.local/bin" "$HOME/.cache/code-server-install"
    TARBALL="$HOME/.cache/code-server-install/code-server-${CODE_VERSION}-linux-amd64.tar.gz"
    if [ ! -f "$TARBALL" ]; then
        curl -fsSL -o "$TARBALL" \
            "https://github.com/coder/code-server/releases/download/v${CODE_VERSION}/code-server-${CODE_VERSION}-linux-amd64.tar.gz"
    fi
    rm -rf "$HOME/.local/lib/code-server-${CODE_VERSION}"
    tar -xzf "$TARBALL" -C "$HOME/.local/lib"
    ln -sfn "$HOME/.local/lib/code-server-${CODE_VERSION}/bin/code-server" "$BIN"
    echo "installed: $BIN"
fi
"$BIN" --version

# --- 2. server config (password synced from app .env) -----------------------
APP_PASSWORD=""
if [ -f "$ROOT/.env" ]; then
    # shellcheck disable=SC1090
    APP_PASSWORD="$(grep -E '^PASSWORD=' "$ROOT/.env" | tail -1 | cut -d= -f2- || true)"
fi
if [ -z "$APP_PASSWORD" ]; then
    APP_PASSWORD="admin"  # matches server.py default when PASSWORD is unset
    echo "warning: no PASSWORD in .env; using 'admin' (same default as the app)"
fi

CS_DIR="$HOME/.config/code-server"
mkdir -p "$CS_DIR"
CS_CFG="$CS_DIR/config.yaml"
BIND_ADDR="${BIND_HOST}:${CODE_PORT}"
if [ "$CODE_PORT_EXPLICIT" = 0 ] && [ "$BIND_HOST_EXPLICIT" = 0 ] && [ -f "$CS_CFG" ] && grep -Eq '^bind-addr:' "$CS_CFG"; then
    EXISTING="$(grep -E '^bind-addr:' "$CS_CFG" | tail -1 | awk '{print $2}')"
    case "$EXISTING" in
        127.0.0.1:8080|127.0.0.1:8600) ;;  # known loopback defaults: migrate out
        *) BIND_ADDR="$EXISTING"; echo "keeping existing bind-addr: $BIND_ADDR";;
    esac
fi
printf 'bind-addr: %s\nauth: password\npassword: %s\ncert: false\n' \
    "$BIND_ADDR" "$APP_PASSWORD" > "$CS_CFG"
chmod 600 "$CS_CFG"
echo "wrote $CS_CFG (bind ${BIND_ADDR}, password synced from app .env)"

# --- 3. Office Viewer extension --------------------------------------------
if "$BIN" --list-extensions 2>/dev/null | grep -qi '^cweijan.vscode-office'; then
    echo "extension already installed: cweijan.vscode-office"
else
    echo "installing Office Viewer extension (docx/xlsx/pptx)…"
    "$BIN" --install-extension cweijan.vscode-office || \
        echo "warning: extension install failed (offline?) — install later with: $BIN --install-extension cweijan.vscode-office"
fi

# --- 4. managed program entry ----------------------------------------------
python3 - "$ROOT/config.yaml" "$BIND_ADDR" <<'EOF'
import sys
import yaml

cfg_path, bind_addr = sys.argv[1], sys.argv[2]
with open(cfg_path) as f:
    cfg = yaml.safe_load(f) or {}
programs = cfg.get("programs") or []
if any(isinstance(p, dict) and p.get("name") == "code-server" for p in programs):
    print("managed program already registered: code-server")
    sys.exit(0)
import os as _os
programs.append({
    # cwd must be absolute: the process manager passes it straight
    # to Popen (no tilde expansion there; only the shell command
    # string gets $HOME).
    "name": "code-server",
    "command": f"$HOME/.local/bin/code-server --bind-addr {bind_addr} --auth password --disable-telemetry",
    "cwd": _os.path.expanduser("~"),
    "autostart": True,
})
cfg["programs"] = programs
with open(cfg_path, "w") as f:
    yaml.safe_dump(cfg, f, default_flow_style=False, sort_keys=False)
print("registered managed program: code-server (autostart)")
EOF

echo
echo "Done. Restart the app and open /code:"
echo "  ./stop.sh && ./start.sh"
