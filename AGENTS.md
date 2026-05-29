# Server Services Manager — Agent Guide

## Quick start
```bash
pip install -r requirements.txt
echo "PASSWORD=your_secure_password" > .env   # also supports SECRET_KEY, CORS_ORIGIN
./start.sh                                     # starts on port 8881, logs to server.log
./stop.sh                                      # uses .server.pid
```

## Architecture
- **Entrypoint:** `server.py` — single Flask app with SocketIO, eventlet monkey-patch at line 4-5
- **Routes** (`server.py`): `/` dashboard, `/monitor` system monitor, `/programs/*` CRUD, `/api/files/*` file manager, `/login`, `/logout`
- **WebSocket events:** `update` (program cards, 1s interval), `system_stats` (monitor, 2s), `terminal_*`
- **Backend modules:** `app/process_manager.py` (Program, ProcessManager), `app/terminal_manager.py` (PTY sessions)
- **Frontend:** All inline JS in `templates/index.html` (~1156 lines, 4 script blocks). Monitor at `templates/monitor.html` uses Chart.js. No build step, no framework.
- **Config:** `config.yaml` (gitignored) defines services; can also be managed via UI
- **State persistence:** Running PIDs saved to `~/.server-services-manager/state.json`, re-attached on restart

## Key gotchas
- **eventlet + Python 3.13:** `eventlet.monkey_patch()` conflicts with `httpcore>=1.0` + `trio`. Pin `httpcore<1.0` if import fails. `start.sh` already sets `PYTHONWARNINGS="ignore"` to suppress eventlet deprecation.
- **CPU normalization:** `psutil.cpu_percent(interval=0)` already returns 0-100% system-wide — do NOT divide by num_cores. For process CPU, `proc.cpu_percent()` returns per-core (0-100% of one core); divide by `num_cores` to get total-system share.
- **CPU temperature:** `psutil.sensors_temperatures()` may not exist on all systems. Handle gracefully — try `coretemp`, `k10temp`, `cpu-thermal`, `thinkpad`, `acpitz` as fallbacks.
- **Auto-restart backoff:** `start()` does NOT reset `restart_count` — intentional. Counter increments via `_handle_restart()`. Resets on `stop()` or after 60s of uptime (`process_manager.py:224`).
- **SECRET_KEY:** Auto-generated via `os.urandom(24)` if not set in env. No need to configure.
- **Password:** Hashed at startup. Default `admin` if `PASSWORD` not set.
- **Log buffer:** `collections.deque(maxlen=10000)` per program. WebSocket sends last 100 lines every 1s.
- **Healthcheck:** `GET /health` bypasses auth.
- **Monitor polling:** `system_monitor_thread()` emits `system_stats` every 2s. 60-point history (~2min) stored client-side for chart rendering.
- **Disk aggregation:** Monitor filters out `/snap/*`, `/boot/efi`, and `/dev/loop*` partitions and aggregates physical disk totals.
- **Network aggregation:** Monitor filters out `docker*`, `veth*`, `br-*`, `virbr*` interfaces and aggregates physical speeds.
- **Control panel:** `/control` page with whitelisted system commands (Power/System/Network groups). Destructive commands (shutdown, reboot, suspend, hibernate) require confirmation + password re-auth. Custom commands can be added via `commands:` in `config.yaml`:

  ```yaml
  commands:
    - id: my-update
      name: "Update System"
      command: "apt update && apt upgrade -y"
      icon: refresh-cw
      auth: true
  ```

## Testing
```bash
python -m pytest tests/ -v --tb=short    # 35 tests
```
- Tests use `unittest.mock` to avoid real subprocesses
- Fixtures in `tests/conftest.py` provide `temp_config` (yaml), `process_manager`, `program_config`
- No integration tests against the live server (no test client for Flask-SocketIO)
- GitHub Actions workflow at `.github/workflows/test.yml` runs ruff lint + pytest on push/PR (Python 3.10-3.13)

## Edit-service rename bug (fixed)
When editing a service name, the PUT request URL uses the `original-name` hidden field (`templates/index.html:256`), not the new name from the form.

## File manager path checks
All file endpoints (`/api/files/*`) chroot to `$HOME`. Use `os.path.realpath()` comparison — symlinks outside home are blocked.

## Dependencies
- `flask`, `flask-socketio`, `eventlet`, `pyyaml`, `python-dotenv`, `psutil`
- Frontend CDNs: Tailwind, Socket.IO, Lucide icons, xterm.js, Chart.js
