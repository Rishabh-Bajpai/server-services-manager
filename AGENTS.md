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
- **Routes** (`server.py`): `/` dashboard, `/monitor` system monitor, `/system-services` systemd units, `/docker` docker containers, `/cron` system cron jobs, `/notifications` health-check dashboard, `/alerts` notification delivery log, `/activity` activity log, `/control` whitelisted system commands, `/programs/*` CRUD, `/api/files/*` file manager, `/api/system-services/*`, `/api/docker/*`, `/api/programs/<name>/{autostart,schedule,limits}`, `/api/palette/search`, `/api/alerts/*`, `/api/plugins`, `/login`, `/logout`, `/health` (no auth)
- **WebSocket events:** `update` (program cards, 1s interval), `system_stats` (monitor, 2s), `terminal_*`, `service_event` (state-change toasts), `health_event`
- **Backend modules:**
  - `app/process_manager.py` — Program, ProcessManager, ProgramConfig (name/command/cwd/autostart/schedule/environment), `on_state_change` hook fired on every transition
  - `app/terminal_manager.py` — PTY sessions
  - `app/system_services.py` — systemd unit introspection/control via `systemctl` (read + cached; write requires sudo password). `get_dependencies()` returns the BFS graph for `/system-services/<name>/graph`.
  - `app/log_streamer.py` — reference-counted `journalctl -f` per (unit, priority) with per-subscriber queues; SSE endpoint
  - `app/cron_manager.py` — parse /etc/crontab and /etc/cron.d/*, validate 5-field expressions, enable/disable via `sudo cp` of tempfile
  - `app/health.py` — HealthMonitor background thread (http/tcp/cmd checks, state tracking, transition events)
  - `app/notifier.py` — pluggable notifiers (ntfy, webhook, telegram, smtp email), `Event` dataclass, `fanout()`
  - `app/activity.py` — SQLite WAL DB at `~/.server-services-manager/activity.db`, `log()` is best-effort and never raises
  - `app/palette.py` — subsequence fuzzy scorer (word-boundary + prefix + length bonuses)
  - `app/schedules.py` — systemd timer units (ssm-<name>.service + .timer) for cron-style scheduled tasks
  - `app/resource_limits.py` — systemd drop-in overrides (CPUQuota, MemoryMax, TasksMax, IOWeight, Nice, etc.) at `~/.config/systemd/user/ssm-<name>.service.d/99-manager.conf`
  - `app/log_persistence.py` — per-service log tail persisted to `~/.server-services-manager/logs/<name>.log` (rotated at 512KB)
  - `app/config_schema.py` — pydantic schema for `config.yaml` (lenient `extra="allow"`, name regex, unique-name enforcement, notifier type discriminators)
  - `app/plugins.py` — drop-in Python plugin loader, `PluginBase` ABC, `load_all()` runs at startup
  - `app/docker_manager.py` — Docker SDK wrapper (list/inspect/logs/stats/control); `is_available()` returns availability + reason; logs streaming via SSE `/api/docker/containers/<id>/logs/stream`
  - `app/alert_log.py` — Notification delivery log (separate `notification_events` table in activity.db); `fanout_with_logging()` is a drop-in replacement for `notifier.fanout()` that records each delivery's success/failure, latency, channel, recipient; per-channel stats aggregation
- **Frontend:** All inline JS in `templates/index.html` (~1700 lines, 4 script blocks). Monitor at `templates/monitor.html` uses Chart.js. System services at `templates/system-services.html` (~600 lines, side-panel detail view with Overview/Dependencies/Unit File/Logs tabs). Docker at `templates/docker.html` (containers table + side panel with Details/Stats/Logs tabs, live log streaming). Cron at `templates/cron.html`, notifications at `templates/notifications.html`, activity at `templates/activity.html`, control at `templates/control.html`. No build step, no framework.
- **Config:** `config.yaml` (gitignored) defines services; can also be managed via UI
- **State persistence:** Running PIDs saved to `~/.server-services-manager/state.json`, re-attached on restart

## Key gotchas
- **eventlet + Python 3.13:** `eventlet.monkey_patch()` conflicts with `httpcore>=1.0` + `trio`. Pin `httpcore<1.0` if import fails. `start.sh` already sets `PYTHONWARNINGS="ignore"` to suppress eventlet deprecation.
- **CPU normalization:** `psutil.cpu_percent(interval=0)` already returns 0-100% system-wide — do NOT divide by num_cores. For process CPU, `proc.cpu_percent()` returns per-core (0-100% of one core); divide by `num_cores` to get total-system share.
- **CPU temperature:** `psutil.sensors_temperatures()` may not exist on all systems. Handle gracefully — try `coretemp`, `k10temp`, `cpu-thermal`, `thinkpad`, `acpitz` as fallbacks.
- **Auto-restart backoff:** `start()` does NOT reset `restart_count` — intentional. Counter increments via `_handle_restart()`. Resets on `stop()` or after 60s of uptime (`process_manager.py:_handle_restart`).
- **SECRET_KEY:** Auto-generated via `os.urandom(24)` if not set in env. No need to configure.
- **Password:** Hashed at startup. Default `admin` if `PASSWORD` not set.
- **Log buffer:** `collections.deque(maxlen=10000)` per program + per-service tail persisted to `~/.server-services-manager/logs/<name>.log` (rotated at 512KB). WebSocket sends last 100 lines every 1s.
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

- **System Services page** (`/system-services`): browse and control all systemd units on the host. Read operations (`list`, `show`, `cat`, `journalctl`) work without privileges. Write operations (start, stop, restart, reload, enable, disable, mask, daemon-reload, edit) require the user's sudo password — the same password they use to log in. The password is piped to `sudo -S` for that single command; nothing else is escalated. **The Flask process itself does not need to run as root** — only the user invoking the action needs sudo. Drop-in edits write to `/etc/systemd/system/<name>.d/99-manager.conf` (the standard `systemctl edit` location) and automatically `daemon-reload` afterwards, so vendor unit files are never touched. Dependencies tab uses `get_dependencies(name, depth)` BFS over Requires/Wants/TriggeredBy/After/Before, capped at 40 nodes (systemd-journald alone has 100+ After/Before edges) with `_GRAPH_SKIP` for well-known targets.

  Unit types exposed: `service`, `timer`, `socket`, `path`, `mount`. Long lists (500+ units) are handled via `table-layout: fixed` columns and a horizontally scrollable table wrapper; the side panel loads detail on click.

  ```python
  from app import system_services
  units = system_services.list_units(unit_type="service", state="active", search="nginx")
  detail = system_services.get_unit("cron.service")
  graph = system_services.get_dependencies("NetworkManager.service", depth=2)
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

- **Docker page** (`/docker`): browse and control Docker containers on the host. Uses the Python `docker` SDK (not the CLI). `is_available()` probes `/var/run/docker.sock` and returns a human-readable reason when the daemon is unreachable (typical cases: socket missing, daemon stopped, or the current user not in the `docker` group — in which case the UI shows the exact `sudo usermod -aG docker $USER` command). All read operations (list, inspect, logs tail, stats snapshot) work without root. Lifecycle actions (start, stop, restart, kill, pause, unpause) and remove also go through the socket — no sudo needed if the user is in the docker group. Logs tab streams live output via SSE (same pattern as systemd unit logs); Stats tab polls every 2s and renders CPU/memory/network/disk bars.

  ```python
  from app import docker_manager
  docker_manager.is_available()    # -> {"available": True, "reason": "", "version": "24.0.7"}
  for c in docker_manager.list_containers(all_containers=True):
      print(c.name, c.state, c.is_running)
  docker_manager.control("web", "restart")
  docker_manager.get_stats("web")
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

- **Program state-change hook** (`app/process_manager.py:Program._set_status`): every state transition calls `program.on_state_change(program, old, new)`. server.py attaches a single global hook (`_on_program_state_change`) that fans the transition out to all notifiers in a daemon thread and pushes a `service_event` to socketio for UI toasts. The hook is wired at startup; new programs added via API also get the hook attached (`server.py:api_add_program`). The notifier fanout runs in a real OS thread so a slow webhook can't stall the program thread.

  ```python
  program.on_state_change = lambda p, old, new: print(f"{p.config.name}: {old.value} -> {new.value}")
  ```

- **Scheduled tasks** (`app/schedules.py`): a managed program can have a `schedule:` field in config.yaml. When set, the manager creates `~/.config/systemd/user/ssm-<name>.service` (one-shot) + `ssm-<name>.timer` (OnCalendar + Persistent) and enables the timer. The dashboard's Start button still spawns the subprocess directly, so manual triggers keep working. The OnCalendar validator is a lightweight regex (`[A-Za-z0-9*.,/\-\s:]+`) — it rejects shell metacharacters and `//` but accepts standard cron extensions like `Mon..Fri 09:00:00`. The unit is created with `--user` so no root is needed; sudo only for write ops with the app password. API:

  ```python
  from app import schedules
  schedules.write_units("api", "/usr/bin/foo", "/tmp", "hourly",
                       environment={"KEY": "VAL"})
  schedules.enable_timer("api", password=user_password)
  schedules.trigger_now("api", password=user_password)
  schedules.timer_status("api")
  ```

- **Resource limits** (`app/resource_limits.py`): per-service systemd drop-in at `~/.config/systemd/user/ssm-<name>.service.d/99-manager.conf`. Supported fields: `cpu_quota` (0-100%), `memory_max`/`memory_high` (e.g. `512M`, `2G`, `infinity`), `tasks_max`, `io_weight`/`cpu_weight` (1-10000), `limit_nofile`, `nice` (-20..19). Each field has type-specific validation. Empty value clears that one field. `apply()` merges with existing settings; `clear()` removes the drop-in. API:

  ```python
  from app import resource_limits
  resource_limits.apply("api", {"cpu_quota": "50", "memory_max": "512M"},
                        password=user_password)
  resource_limits.get("api")    # -> {"cpu_quota": "50", "memory_max": "512M"}
  resource_limits.clear("api", password=user_password)
  ```

- **Log persistence** (`app/log_persistence.py`): every line appended to `Program.logs` is also written to `~/.server-services-manager/logs/<safe-name>.log` (rotated at 512KB, last half kept). Available via `GET /programs/<name>/logs/tail` (default 200 lines) and merged into `/programs/<name>/logs/download`. `DELETE /programs/<name>/logs` clears both. Best-effort — persistence failure never blocks the program.

- **Command palette** (`app/palette.py`): Ctrl+K opens a fuzzy-search modal. Sources: managed programs, control commands, system units. `_score()` uses subsequence match + word-boundary bonus + prefix bonus + length penalty. Index is rebuilt on each `build_palette_index()` call. `GET /api/palette/search?q=...` returns the top 20 results.

- **Activity log** (`app/activity.py`): SQLite WAL DB at `~/.server-services-manager/activity.db`, table `activity(ts, action, target, status, detail, user, ip)`. `log()` is best-effort (`try/except` swallows all errors) so a logging failure cannot block an action. `/activity` page shows filterable list + summary; CSV export at `GET /api/activity/export`. Hooks into: program CRUD, systemd actions, cron toggle, autostart, schedule, limits, command palette runs.

- **Config validation** (`app/config_schema.py`): pydantic models for every block (ProgramConfig, HealthCheck, Command, Ntfy/Webhook/Telegram/Email). `validate_config(data)` returns the validated tree or raises `ValidationError` with field paths. Lenient by design: `extra="allow"` everywhere so users can add fields without breaking the schema. `RootConfigSchema` enforces unique program / command / notifier names. Called at startup with `try/except` — bad config logs a warning but doesn't refuse to start.

- **Plugins** (`app/plugins.py`): drop a `.py` file in `~/.server-services-manager/plugins/`. The loader imports each file, finds subclasses of `PluginBase`, instantiates them, and calls `register(app=app, pm=pm, activity=activity, notifier_module=notifier)`. Auto-reload on file change is NOT supported (too dangerous); restart required. A bad plugin is logged + skipped; it can't break the manager. `GET /api/plugins` returns discovery metadata for the UI. `PluginBase.unregister()` is the optional cleanup hook called at shutdown.

  ```python
  from app.plugins import PluginBase

  class Hello(PluginBase):
      name = "hello"
      version = "1.0"
      def register(self, app, pm, activity, notifier_module=None):
          @app.route("/_hello")
          def hello():
              return "world"
  ```

## Secret-leak guardrails

The repo is public, so anything that pins to the user's specific
machine — home directory paths, conda env names, program names
they run, file contents of `config.yaml`, or the actual app
password — must not be committed. The `tools/` directory has
the scripts to keep that discipline:

- `tools/secrets.txt` (gitignored) — the user-local list of
  patterns to detect and scrub. Copy `tools/secrets.txt.example`
  here and add your specifics. Both the guard and the scrubber
  load this file at startup; the tool source itself contains
  no pattern strings, so the public repo doesn't leak them.
- `tools/check-secrets.py [staged|all]` — scan the working tree
  for the patterns; exit non-zero if any match. Wired into
  `.github/workflows/test.yml` so PRs fail the check. The tool
  itself and the secrets file are excluded from the scan to
  avoid false positives.
- `tools/scrub.py` — text-replacement driver used by the
  history-rewrite script. Loads the same patterns as
  `check-secrets.py` and runs the same regex substitutions.
- `tools/clean-history.sh [--all]` — `git filter-branch` driver
  that scrubs blob content and commit messages through
  `scrub.py`, then removes `config.yaml` / `config_example.yaml`
  / `start_process_manager.sh` from every commit. Run only
  when a leak is found in an old commit. By default rewrites
  only the current branch; pass `--all` to rewrite every
  branch.

Adding a new pattern: edit `tools/secrets.txt.example` (so a
new contributor knows the format) and your local
`tools/secrets.txt`.

## Testing
```bash
python -m pytest tests/ -v --tb=short    # 415 tests
```
- Tests use `unittest.mock` to avoid real subprocesses
- Fixtures in `tests/conftest.py` provide `temp_config` (yaml), `process_manager`, `program_config`
- No integration tests against the live server (no test client for Flask-SocketIO)
- `tests/test_system_services.py` includes both mocked tests and live integration tests for `systemctl list-units` and `systemctl show`
- GitHub Actions workflow at `.github/workflows/test.yml` runs ruff lint + pytest on push/PR (Python 3.10-3.13)

## Edit-service rename bug (fixed)
When editing a service name, the PUT request URL uses the `original-name` hidden field (`templates/index.html`), not the new name from the form.

## File manager path checks
All file endpoints (`/api/files/*`) chroot to `$HOME`. Use `os.path.realpath()` comparison — symlinks outside home are blocked.

## Dependencies
- `flask`, `flask-socketio`, `eventlet`, `pyyaml`, `python-dotenv`, `psutil`, `pydantic`
- Frontend CDNs: Tailwind, Socket.IO, Lucide icons, xterm.js, Chart.js
