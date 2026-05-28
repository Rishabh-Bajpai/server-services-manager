# Server Services Manager — Agent Guide

## Quick start
```bash
pip install -r requirements.txt
echo "PASSWORD=your_secure_password" > .env   # also supports SECRET_KEY, CORS_ORIGIN
./start.sh                                     # starts on port 8881
./stop.sh                                      # uses .server.pid
```

## Architecture
- **Entrypoint:** `server.py` — single Flask app with SocketIO, eventlet monkey-patch at line 4-5
- **Backend modules:** `app/process_manager.py` (Program, ProcessManager), `app/terminal_manager.py` (PTY sessions)
- **Frontend:** All inline JS in `templates/index.html` (~920 lines, 4 script blocks). No build step, no framework.
- **Config:** `config.yaml` (gitignored) defines services; can also be managed via UI
- **State persistence:** Running PIDs saved to `~/.server-services-manager/state.json`, re-attached on restart

## Key gotchas
- **eventlet + Python 3.13 compatibility:** `eventlet.monkey_patch()` conflicts with `httpcore>=1.0` + `trio`. Pin `httpcore<1.0` if import fails. Workaround: `PYTHONWARNINGS="ignore"` suppresses eventlet deprecation warning.
- **Auto-restart backoff:** `start()` does NOT reset `restart_count` — that's intentional. Counter accumulates via `_handle_restart()`. Reset happens on `stop()` (manual intervention) or after 60s of uptime.
- **SECRET_KEY:** Auto-generated via `os.urandom(24)` if not set in env. No need to configure unless session persistence across restarts is needed.
- **Password:** Hashed at startup with `werkzeug.security.generate_password_hash`. Default is `admin` if `PASSWORD` not set.
- **Log buffer:** `collections.deque(maxlen=10000)` per program. WebSocket sends last 100 lines every 1s.
- **Healthcheck:** `GET /health` bypasses auth — useful for monitoring.
- **systemd unit:** `server-services-manager.service` is a template. Edit paths before installing.

## Testing
```bash
python -m pytest tests/ -v --tb=short    # 35 tests
```
- Tests use `unittest.mock` to avoid real subprocesses
- Fixtures in `tests/conftest.py` provide `temp_config` (yaml), `process_manager`, `program_config`
- No integration tests against the live server (no test client for Flask-SocketIO)
- GitHub Actions workflow at `.github/workflows/test.yml` runs ruff lint + pytest on push/PR

## Edit-service rename bug (fixed)
When editing a service name, the PUT request URL uses the `original-name` hidden field, not the new name from the form. Frontend: `templates/index.html:648`.

## File manager path checks
All file endpoints (`/api/files/*`) chroot to `$HOME`. Use `os.path.realpath()` comparison — symlinks outside home are blocked.
