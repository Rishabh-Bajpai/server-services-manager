# Server Services Manager — Agent Guide

## Quick start
```bash
pip install -r requirements.txt
echo "PASSWORD=your_secure_password" > .env   # also supports SECRET_KEY, CORS_ORIGIN
./start.sh                                     # starts on port 8881, logs to server.log
./stop.sh                                      # uses .server.pid

# Or run as a persistent systemd service:
bash install-service.sh
journalctl --user -u server-services-manager -f
```

## Architecture
- **Entrypoint:** `server.py` — single Flask app with SocketIO, eventlet monkey-patch at line 4-5
- **Routes** (`server.py`): `/` dashboard, `/monitor` system monitor, `/system-services` systemd units, `/programs/*` CRUD, `/api/files/*` file manager, `/api/system-services/*`, `/login`, `/logout`
- **WebSocket events:** `update` (program cards, 1s interval), `system_stats` (monitor, 2s), `terminal_*`
- **Backend modules:**
  - `app/process_manager.py` — Program, ProcessManager (managed services in `config.yaml`)
  - `app/terminal_manager.py` — PTY sessions
  - `app/system_services.py` — systemd unit introspection/control via `systemctl` (read + cached; write requires sudo password)
- **Frontend:** All inline JS in `templates/index.html` (~1160 lines, 4 script blocks). Monitor at `templates/monitor.html` uses Chart.js. System services at `templates/system-services.html` (~600 lines, side-panel detail view). No build step, no framework.
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

- **System Services page** (`/system-services`): browse and control all systemd units on the host. Read operations (`list`, `show`, `cat`, `journalctl`) work without privileges. Write operations (start, stop, restart, reload, enable, disable, mask, daemon-reload, edit) require the user's sudo password — the same password they use to log in. The password is piped to `sudo -S` for that single command; nothing else is escalated. **The Flask process itself does not need to run as root** — only the user invoking the action needs sudo. Drop-in edits write to `/etc/systemd/system/<name>.d/99-manager.conf` (the standard `systemctl edit` location) and automatically `daemon-reload` afterwards, so vendor unit files are never touched.

  Unit types exposed: `service`, `timer`, `socket`, `path`, `mount`. Long lists (500+ units) are handled via `table-layout: fixed` columns and a horizontally scrollable table wrapper; the side panel loads detail on click.

  ```python
  from app import system_services
  units = system_services.list_units(unit_type="service", state="active", search="nginx")
  detail = system_services.get_unit("cron.service")
  system_services.control("cron.service", "restart", password=user_password)
  ```

- **Live log streaming** (`app/log_streamer.py`): the Logs tab on the system services page supports real-time streaming via Server-Sent Events. The backend spawns a single `journalctl -f` per (unit, priority) and fans lines out to per-subscriber queues; the frontend opens an `EventSource` on `/api/system-services/<name>/logs/stream`. Subprocess management is reference-counted: when the last subscriber unsubscribes, the journalctl process is terminated. The reader runs in a real OS thread (not an eventlet greenlet) so blocking `readline()` doesn't trip the eventlet hub, but `process.wait()` is intentionally not called during teardown to avoid the same conflict.

  ```python
  from app.log_streamer import get_streamer
  streamer = get_streamer()
  handle = streamer.subscribe("cron.service", "sid1", priority="info", lines=100)
  queue = streamer.get_subscriber_queue(handle, "sid1")  # consume from this
  ```

- **Cron page** (`/cron`): inspect and toggle system cron jobs in `/etc/crontab` and `/etc/cron.d/*`. Per-user crontabs in `/var/spool/cron/crontabs/<user>` are listed read-only when readable (use `crontab -e` to edit those). The expression validator checks each of the 5 fields against the standard cron range, step, and list syntax; the UI shows the human-readable description and flags invalid input red. Enable/disable works by commenting or uncommenting the line via `sudo cp` of a tempfile; the user's app password is piped to `sudo -S` for that single command, so the Flask process stays unprivileged.

  ```python
  from app import cron_manager
  cron_manager.validate_expression("*/5 * * * *")   # -> None (valid)
  cron_manager.validate_expression("60 * * * *")    # -> "minute: value 60 out of range"
  jobs = cron_manager.list_all()
  cron_manager.toggle_system_job("/etc/cron.d/foo", line_number=3,
                                 enabled=False, password=user_password)
  ```

- **Health checks + notifications** (`app/health.py`, `app/notifier.py`): a `health_check:` block in `config.yaml` describes how to probe each managed service (http / tcp / cmd). A background thread runs all configured checks, tracks each service's last-known state, and emits on transition (healthy <-> unhealthy) via a pluggable notifier (ntfy.sh, generic webhook, Telegram, SMTP email). The `/notifications` page shows a live status dashboard with summary cards and recent events; SocketIO pushes transitions toasts in real time. Configuration:

  ```yaml
  programs:
    - name: api
      command: ...
      health_check:
        type: http             # http | tcp | cmd
        target: http://localhost:8080/health
        interval: 30
        timeout: 5
  notifications:
    - type: ntfy
      topic: alerts
    - type: webhook
      url: https://example.com/hook
  ```

## Testing
```bash
python -m pytest tests/ -v --tb=short    # 197 tests
```
- Tests use `unittest.mock` to avoid real subprocesses
- Fixtures in `tests/conftest.py` provide `temp_config` (yaml), `process_manager`, `program_config`
- No integration tests against the live server (no test client for Flask-SocketIO)
- `tests/test_system_services.py` includes both mocked tests and live integration tests for `systemctl list-units` and `systemctl show`
- GitHub Actions workflow at `.github/workflows/test.yml` runs ruff lint + pytest on push/PR (Python 3.10-3.13)

## Edit-service rename bug (fixed)
When editing a service name, the PUT request URL uses the `original-name` hidden field (`templates/index.html:256`), not the new name from the form.

## File manager path checks
All file endpoints (`/api/files/*`) chroot to `$HOME`. Use `os.path.realpath()` comparison — symlinks outside home are blocked.

## Dependencies
- `flask`, `flask-socketio`, `eventlet`, `pyyaml`, `python-dotenv`, `psutil`
- Frontend CDNs: Tailwind, Socket.IO, Lucide icons, xterm.js, Chart.js
