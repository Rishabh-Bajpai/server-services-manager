# UI Improvements Plan

A focused roadmap of UI/UX enhancements for the server-services-manager
app. Derived from a comprehensive review of all 19 templates and a
backend-vs-frontend feature gap analysis.

> Status legend: `[ ]` pending · `[~]` in progress · `[x]` done

---

## Tier 1 — Highest Impact (Quick wins, missing features)

These expose backend capabilities that are already implemented but
not surfaced in the UI, or fix real bugs the user will hit.

### 1.1 — Listen for `service_event` on dashboard  `[x]`
The server pushes a `service_event` SocketIO event on every program
state transition. The dashboard never listens for it, so toasts are
silently dropped. Wire up a listener that shows a transient toast
when a service changes state (running → failed, etc.).

File: `templates/index.html`

**Status:** Done. Added `socket.on('service_event', ...)` listener
that maps `severity` to a toast style (info→success, warning/critical→error)
and calls a new `toast()` helper. The helper appends a chip to a
`#toast-container` div at top-right (z-50) with the existing
`toastIn`/`toastOut` keyframe animations. Auto-removes after 3s.

### 1.2 — Schedule "Run Now" button + timer status  `[ ]`
The backend already exposes `POST /api/programs/<name>/schedule/trigger`
and the schedule GET returns `timer: {enabled, active, next_run, last_run}`.
The dashboard cards show the schedule text but never show the next run
time, last run, or whether the timer is active — and there's no "Run
now" button.

Plan:
  * Show `Next run: in 4h 23m` under the schedule badge on each card.
  * Add a "Run now" button (clock-arrow icon) next to the schedule badge
    when the program has a schedule.
  * Add a small "X times triggered" counter to the schedule editor.

Files: `templates/index.html`, `app/process_manager.py` (verify the API)

### 1.3 — Docker Images tab  `[ ]`
The backend has `GET /api/docker/images` returning image data, but
`docker.html` only shows containers. Add a third tab "Images" with
a table of images (id, repo:tag, size, created) and a "Remove"
action with confirmation.

Files: `templates/docker.html`, `app/docker_manager.py`

### 1.4 — Backup timer status (next_run / last_run)  `[ ]`
`GET /api/backups/<name>/status` returns `timer: {enabled, active,
next_run, last_run}`. The backups table only shows a static
"enabled/disabled" badge. Show the next run and last run on the
row, and add a "Run now" action button.

Files: `templates/backups.html`, `app/backup_manager.py`

### 1.5 — System-services: `mask` / `unmask` actions  `[ ]`
AGENTS.md lists `mask` and `unmask` as available actions. The UI
doesn't have buttons for them. Add row actions behind a "..."
overflow menu with confirmation (mask is destructive — user can
unbreak a service by mistake).

Files: `templates/system-services.html`, `app/system_services.py`

### 1.6 — Swagger UI link in nav  `[x]`
`/docs/` is reachable but not linked anywhere. Add a "API Docs"
link in the dashboard header (and in `_macros.html` actions slot)
so users can discover the OpenAPI explorer.

Files: `templates/index.html`, `templates/_macros.html`

**Status:** Done. Added a "API" button (book-open icon) in the
dashboard nav row, right after Add Service and before Logout.
Points to `/docs/` with `target="_blank"`. The `_macros.html`
action slot is already there — pages can include the link by
adding it to their `actions` HTML.

Also flipped `app.config['TEMPLATES_AUTO_RELOAD'] = True` in
server.py so template changes are picked up without a restart
during development.

---

## Tier 2 — Backend Gaps (Feature pages that don't exist)

### 2.1 — Plugins page  `[ ]`
`GET /api/plugins` returns discovery metadata for loaded plugins.
There's no page that shows them. Add `/plugins` route + template
that lists each plugin's `name`, `version`, and any registered
routes / hooks. Useful even when zero plugins are loaded (shows
the directory and how to add one).

Files: new `templates/plugins.html`, `app/plugins.py`, `server.py`

### 2.2 — Config validation page  `[ ]`
`app/config_schema.py` has `validate_config()`. No UI exposes it.
Add a "Config" page that calls the validator and renders the
result (validation errors with field paths, or "OK"). Also show
the effective `config.yaml` content (with secrets redacted) in a
read-only viewer.

Files: new `templates/config.html`, `app/config_schema.py`,
`server.py`

### 2.3 — Activity log: `user` and `since` filters  `[ ]`
Backend supports `?user=...&since=...` but the activity page
only exposes `action`, `target`, `status`. Add the missing filter
inputs.

Files: `templates/activity.html`

---

## Tier 3 — Mobile Responsiveness

### 3.1 — Dashboard header collapses to hamburger on mobile  `[ ]`
13+ nav buttons is too many for a mobile top bar. Replace with a
hamburger icon on `<sm` that opens a slide-down menu.

Files: `templates/index.html`

### 3.2 — Touch support for the resizer  `[ ]`
The dashboard's drag-resize handle uses mouse events only. Add
`touchstart`/`touchmove`/`touchend` equivalents so the terminal
pane is resizable on tablets.

Files: `templates/index.html`

### 3.3 — Treemap height adapts to viewport  `[ ]`
`disk.html` sets the treemap to a fixed 480px. Make it `min(60vh,
480px)` or similar so it fits a phone screen.

Files: `templates/disk.html`

### 3.4 — Force tables to fit small screens  `[ ]`
Several pages set a hard `min-width: 920px` on their data tables
(activity, alerts, cron, system-services). Replace with a
"card view" on mobile: each row becomes a stacked card on `<sm`,
restoring the table on `≥sm`.

Files: `templates/activity.html`, `templates/alerts.html`,
`templates/cron.html`, `templates/system-services.html`

### 3.5 — "Show stopped" toggle visible on mobile (docker.html)  `[ ]`
Currently `hidden sm:flex` — needs to be reachable on phones.

Files: `templates/docker.html`

---

## Tier 4 — UX Consistency

### 4.1 — Unify modal/confirm/alert patterns  `[ ]`
Three different patterns in use:
  * `hidden` class toggle (index, backups, files)
  * `.active` class (system-services, control, monitor)
  * `.open` class (cluster)

Plus some pages use `confirm()`, `alert()`, and `prompt()` for
destructive actions instead of the styled modal pattern.

Plan:
  * Add a `ui.confirm()` and `ui.toast()` helper to `_macros.html`.
  * Migrate every `confirm()` to a styled modal.
  * Migrate every `alert()` to a toast.
  * Migrate `prompt()` for passwords to a styled auth modal (with
    the existing pattern from `system-services.html`).
  * Unify modal show/hide to one consistent class toggle.

Files: `templates/_macros.html`, all 19 page templates

### 4.2 — Loading skeletons  `[ ]`
Many pages show a blank table on initial load. Add skeleton rows
(animated grey bars) to activity, alerts, packages, and backup
tables so users see something while the data fetches.

Files: `templates/activity.html`, `templates/alerts.html`,
`templates/packages.html`, `templates/backups.html`

### 4.3 — Debounce filter inputs  `[ ]`
`alerts.html`'s service-name filter and the activity page's search
inputs fire on every keystroke. Add 250ms debounce so the API isn't
hammered.

Files: `templates/alerts.html`, `templates/activity.html`

### 4.4 — Unify toast position  `[ ]`
`top: 1rem` on most pages, `bottom: 1rem` on cluster/disk/files/ssh.
Pick one (top is the convention).

Files: 4 templates

### 4.5 — Replace `alert()` calls in index.html  `[ ]`
Six `alert()` calls in `index.html` (form errors, delete confirm).
Migrate to the toast system.

Files: `templates/index.html`

### 4.6 — Make `login.html` use CSS variables  `[ ]`
Currently uses hardcoded Tailwind colors (`bg-gray-950` etc).
Migrate to the shared `:root` variable system so the login page
matches the rest of the app.

Files: `templates/login.html`

### 4.7 — Unify the dashboard header style  `[ ]`
`index.html`'s header uses a unique `glass-effect` class.
Bring it in line with the `bg-[var(--bg-card)]/60 backdrop-blur-md`
used everywhere else.

Files: `templates/index.html`

---

## Tier 5 — Polish

### 5.1 — Empty terminal hint  `[ ]`
The dashboard's empty terminal pane shows nothing. Add a
"Click + to start a terminal" hint.

Files: `templates/index.html`

### 5.2 — Package search/lookup  `[ ]`
`packages.html` only shows pending updates. Add a "Lookup" tab
that takes an arbitrary package name and shows its installed
version and available upgrade (if any).

Files: `templates/packages.html`, `app/package_manager.py`

### 5.3 — Backup restore guide  `[ ]`
Backups exist but there's no in-UI hint about where the archive
files live or how to restore them. Add an info banner.

Files: `templates/backups.html`

### 5.4 — Add "List all jobs" to packages page  `[ ]`
`GET /api/packages/jobs` returns a list of all install jobs.
Show them in a sidebar so the user can re-open a finished
install's log.

Files: `templates/packages.html`, `app/package_manager.py`

### 5.5 — Make the dependency graph zoomable on touch  `[ ]`
The SVG graph in system-services doesn't support pinch-zoom on
mobile. Add `viewBox`-based zoom on double-tap.

Files: `templates/system-services.html`

### 5.6 — Cluster: mDNS results panel  `[ ]`
`mdnsBrowse()` only logs results to console. Show them in a
modal with name/host/port badges.

Files: `templates/cluster.html`

### 5.7 — Activity: User column  `[ ]`
Backend records `user`; UI doesn't show it. Add the column.

Files: `templates/activity.html`

---

## Deferred

These were considered but pushed for later:

  * GPU monitoring on `/monitor` — would need nvidia-smi / amdgpu
    probing, lots of edge cases.
  * Disk-usage comparison (diff vs last week) — needs persistent
    snapshots; storage cost not justified.
  * `generate SSH key pair` from the UI — security-sensitive, can
    be done from the command line.
  * Per-network-interface breakdown on `/monitor` — too noisy for
    most servers; aggregate is enough.
  * History persistence for monitor charts — 2-min rolling window
    is fine for most uses.
