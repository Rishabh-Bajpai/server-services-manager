# Server Services Manager — Implementation Plan

This document tracks all planned features for the app, organized by priority and
ordered for incremental, testable, committable delivery. Each phase ends with:
working code, passing tests, lint clean, screenshots updated, and one git commit.

## Current State (as of last commit `fa66f36`)

Existing features (all working, all tested):
- Dashboard with managed services (config.yaml)
- Real-time system monitor (CPU/memory/disk/network)
- Web PTY terminal
- File manager (chrooted to $HOME)
- Control panel (whitelisted system commands + custom commands)
- System services page (browse + control any systemd unit on the host)

Test suite: 78 tests passing. Lint clean for new code. Service runs as
`server-services-manager.service` (user-level systemd).

## Roadmap (17 features, organized into phases)

### Phase 1 — Real-time Log Streaming  *(P1, high value)*
Replace the static "Logs" tab on the system services page with a live stream of
journal entries. Most useful, lowest risk.

- New `app/log_streamer.py` — wraps `journalctl -f -u <name> -o short` in a
  Popen per subscriber; emits each line to a SocketIO room
- `GET /api/system-services/<name>/logs/stream` — opens a long-poll/SSE
  channel; client receives `log_line` events
- Frontend: pause/resume toggle, filter by priority (`--priority=err..info`),
  follow-tail vs. preserve-scroll, clear button
- Configurable line buffer size (default 5000)
- Tests: subprocess mocked, line batching, backpressure

### Phase 2 — Cron Job Management  *(P1, fills a real gap)*
Browse, edit, and toggle cron jobs from the UI. Modern systemd timers cover
many cases, but crons are everywhere and editing the syntax by hand is painful.

- New `app/cron_manager.py` — reads `/etc/crontab`, `/etc/cron.d/*`,
  `/var/spool/cron/crontabs/<user>`; writes via temporary file + `crontab -`
- Parse cron expressions with validation; show "next 5 runs" preview
- New "Cron" tab in the system services page; filter to a single user
- Enable/disable by renaming to `*.disabled` (per-user cron) or commenting in
  `/etc/cron.d/*` (with backup)
- Tests: parser, validator, round-trip (parse → write → parse = identity)

### Phase 3 — Health Checks + Notifications  *(P1, monitoring)*
Turn the app from "control panel" into "monitoring": define a check per
managed service; alert on transition.

- New `health_check` block in `config.yaml`:
  ```yaml
  programs:
    - name: api
      command: ...
      health_check:
        type: http      # http|tcp|cmd
        target: http://localhost:8080/health
        interval: 30
        timeout: 5
  ```
- Background thread `health_check_thread()` — runs checks, tracks last-known
  state, emits on transition
- New `app/notifier.py` — pluggable backends: `email` (SMTP), `telegram`,
  `discord`, `ntfy.sh`, `webhook`
- Notification config in `config.yaml` under `notifications:` (list of sinks)
- UI: status dot on dashboard cards (green/yellow/red/grey); transition toasts;
  new `/notifications` page to view history (in-memory or sqlite)
- Tests: each check type, transition logic, formatter for each notifier

### Phase 4 — Service Dependencies Graph  *(P2, debugging aid)*
Visualize `Requires=` / `Wants=` / `After=` / `Before=` for a unit.

- New `GET /api/system-services/<name>/graph` — returns nodes + edges
  (transitive, depth 2, scoped to units within 5 hops of the requested one)
- New tab in side panel: lightweight SVG rendering (no extra library); click a
  node to switch the panel
- Tests: graph traversal, cycle handling, depth limit

### Phase 5 — Auto-start on Boot (systemd drop-in)  *(P2)*
The `autostart: true` flag in `config.yaml` only controls whether the manager
launches a service on its own startup. Add a real systemd-level mechanism so
the service also starts at system boot.

- New "Start at boot" toggle on each managed service card
- Creates a drop-in at `/etc/systemd/system/<manager-managed>.service.wants/<name>.service`
  linking to the service definition in `config.yaml` (via a generated systemd
  unit that exec's the command)
- Or simpler: register a `oneshot` service that the user enables
- Tests: drop-in generation, enable/disable round-trip

### Phase 6 — Activity Log / Audit Trail  *(P2, multi-user safety)*
Shared servers need to know "who did what when".

- SQLite-backed `activity` table: timestamp, user, action, target, status
- Hook all mutation endpoints (programs CRUD, system services actions, control
  panel runs, file uploads, edits)
- New `/activity` page with filter (by user/action/date/target), CSV export
- Tests: schema, insert, query filters, CSV export

### Phase 7 — Command Palette (Ctrl+K)  *(P2, UX)*
Universal "do anything fast" UI.

- New modal opened with Ctrl+K from any page
- Fuzzy search across: managed services, systemd units, control panel commands,
  pages
- Enter to execute; arrow keys to navigate
- Tests: fuzzy match scoring, action dispatch

### Phase 8 — Scheduled Tasks UI  *(P3, complements cron)*
Convert a managed service into a `timer+service` pair, schedule from the UI.

- Schedule form: "every N min", "daily at HH:MM", or raw `OnCalendar=`
- Generates `<name>.timer` and `<name>.service` drop-ins (sibling to the
  existing one) in the user's systemd or `/etc/systemd/system`
- Tests: schedule parser, generated unit file validation

### Phase 9 — Backup / Restore Managed Configs  *(P3)*
One-click export of all `config.yaml` services + their current logs (last
1000 lines each) as a JSON/zip bundle. Import on a fresh install.

- `GET /api/programs/export` — returns `app.json` with all services + logs
- `POST /api/programs/import` — uploads the same shape, merges or replaces
- UI: buttons in the dashboard header, with confirm dialog for destructive
  import
- Tests: export shape, import round-trip, conflict resolution

### Phase 10 — Per-service Resource Limits (cgroups)  *(P3)*
Set `CPUQuota=`, `MemoryMax=`, `IOWeight=` per managed service via the existing
drop-in editor with a form-based quick-set.

- Quick-set form in addition to freeform text in the drop-in editor
- Pre-filled suggestions based on the host's resources
- Tests: limit validation, generated drop-in content

### Phase 11 — Webhooks on Service Events  *(P3)*
When a managed service starts/stops/fails, POST to a configured URL.

- `webhook:` block in `config.yaml` per service
- POSTed payload: `{service, event, timestamp, hostname, exit_code}`
- Retry with exponential backoff, 3 attempts
- Tests: payload shape, retry, signature header

### Phase 12 — Multi-host / Fleet View  *(P3, large feature)*
See all your servers in one dashboard.

- A coordinator (this app) with `fleet.yml` listing peer URLs + auth tokens
- Aggregated state view, push actions, simple RBAC
- Out of scope for v1 unless explicitly requested — see notes below

### Phase 13 — Profile-based Commands  *(P3)*
A "Profile" is a sequence of commands: `backup → restart → verify → notify`.
Run from the control panel.

- `profiles:` block in `config.yaml` listing ordered steps
- Steps can reference existing commands (whitelisted or custom) or be inline
- Execution view with per-step status; abort on first failure (configurable)
- Tests: step ordering, failure handling, timeout per step

### Phase 14 — Environment File Editor  *(P3)*
Edit a managed service's `environment:` block via form, persist to
`config.yaml`. No more hand-editing YAML for the common case.

- New modal opened from the dashboard service card
- KEY=value rows with add/remove; save writes back to `config.yaml`
- Tests: env parsing, merge with existing, write back

### Phase 15 — Config Schema Validation  *(P3, internal quality)*
Use `pydantic` to validate `config.yaml` and surface clear errors.

- `app/config_schema.py` — models for `ProgramConfig`, `commands`, future
  blocks
- Fail-fast at startup; show in UI when an entry is invalid
- Tests: valid configs pass, invalid configs raise with clear messages

### Phase 16 — Per-service Log Persistence  *(P3, internal quality)*
Managed services' in-memory log deque is lost on restart. Persist the tail to
disk under `~/.server-services-manager/logs/<name>.log`.

- On `log()` write, also append to disk (with size cap)
- On `load_state_and_reattach()`, load the persisted tail
- Tests: rotation, cap, race conditions

### Phase 17 — Plugin System  *(P3, extensibility)*
Drop a Python file in `app/plugins/`; register a route, get a sidebar entry.

- `app/plugins/__init__.py` — discovers, imports, calls a `register(app)` hook
- Plugin manifest: `name`, `version`, `routes`, `nav_entry`
- Examples included: `docker` (list containers, start/stop), `nginx` (reload,
  show config), `postgres` (list DBs)
- Tests: discovery, registration, isolated failures

## Out of Scope (won't build)

- User accounts / multi-tenancy — adds huge complexity
- Container management UI — K8s/Docker already have UIs
- Cluster management — covered by tools like Nomad/Consul
- Secret management — sensitive; offload to Vault
- Fleet view (Phase 12) — by default deferred; only build if asked

## Process

For each phase:
1. Read this file, identify the phase, refresh the plan if scope changed
2. Create a TODO list for the phase's subtasks
3. Implement backend (module + tests)
4. Wire into server.py and templates
5. Run `pytest tests/ -v --tb=short` — must pass
6. Run `ruff check app/<new> tests/test_<new>` — must be clean
7. Restart service, manually verify with curl + browser screenshot
8. Update AGENTS.md / README.md if architecture changed
9. Stage and commit with a clear message
10. Mark the phase done in the in-session todo list

## Commit Message Convention

```
<type>(<scope>): <short description>

<optional body>

Types: feat | fix | refactor | docs | test | chore
Scopes: services | monitor | control | files | core | docs | tests
```
