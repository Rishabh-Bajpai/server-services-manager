# UI Improvements Plan

A focused roadmap of UI/UX enhancements for the server-services-manager
app. Derived from a comprehensive review of all 19 templates and a
backend-vs-frontend feature gap analysis.

> Status legend: `[ ]` pending · `[~]` in progress · `[x]` done

---

## Tier 1 — Highest Impact (Quick wins, missing features)

These expose backend capabilities that are already implemented but
not surfaced in the UI, or fix real bugs the user will hit.

### 1.1 — Listen for `service_event` on dashboard  [x]`[x]`
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

### 1.2 — Schedule "Run Now" button + timer status  [x]`[x]`
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

**Status:** Done.
  * Background thread in `server.py` now also emits `schedule` and
    `timer` (the systemd timer state dict) for every program on every
    `update` event, so the dashboard card has the data to render.
  * Card header shows a small "Run now" button (play icon, blue) next
    to the schedule badge — only when the program has a schedule.
  * Resource bar shows a "Next run" chip (zap icon, purple) with a
    friendly relative label like "in 4h 23m" / "in 2d 5h" / "overdue"
    / "schedule inactive". Format is computed in `formatNextRun()`.
  * `triggerScheduled(name)` posts to
    `/api/programs/<name>/schedule/trigger` with the app password
    and toasts the result (uses the new toast system from 1.1).
  * Bonus fix: `app/schedules.py:_run_systemctl` was passing
    `(password + "\n").encode()` (bytes) into a `text=True` subprocess
    call, which raised `AttributeError: 'bytes' object has no
    attribute 'encode'`. Fixed by passing a str. Updated the existing
    test in `tests/test_schedules.py:TestRunSystemctl` to assert the
    new contract (str input).

### 1.3 — Docker Images tab  [x]`[x]`
The backend has `GET /api/docker/images` returning image data, but
`docker.html` only shows containers. Add a third tab "Images" with
a table of images (id, repo:tag, size, created) and a "Remove"
action with confirmation.

Files: `templates/docker.html`, `app/docker_manager.py`

**Status:** Done.
  * `templates/docker.html`:
      - Top-level view switcher (Containers / Images) above the
        main grid.
      - New Images view with a table: Repository:Tag, ID, Created,
        Size, Actions. Includes a search/filter input and a row
        count chip ("121 images").
      - Per-row delete (trash icon) that confirms and calls the
        new DELETE endpoint.
      - `escapeHtml()` helper added (was missing — renderImages
        was throwing on first render).
  * `app/docker_manager.py`:
      - `remove_image(id_or_name, force=False)` that handles
        short id, full id, and repo:tag. Resolves via the image
        list when a direct remove raises not-found (some short
        ids collide when truncated).
      - `list_images()` now parses ``Created`` from RFC 3339
        (Docker's actual format) into a Unix timestamp, so the
        UI doesn't have to redo the work in JS.
  * `server.py`:
      - New `DELETE /api/docker/images/<path:id_or_name>` route
        that calls `remove_image` and logs to the activity log.
  * Tests: +3 in `tests/test_docker_manager.py` covering the new
    RFC 3339 parsing path, the unparseable fallback, and three
    remove_image scenarios (direct, resolved via list, not-found).

### 1.4 — Backup timer status (next_run / last_run)  [x]`[x]`
`GET /api/backups/<name>/status` returns `timer: {enabled, active,
next_run, last_run}`. The backups table only shows a static
"enabled/disabled" badge. Show the next run and last run on the
row, and add a "Run now" action button.

Files: `templates/backups.html`, `app/backup_manager.py`

**Status:** Done.
  * `templates/backups.html`:
      - `load()` now fetches `/api/backups/<name>/status` for each
        job in parallel and stores the `timer` object on the row.
      - `render()` adds a small "Next run: in 4h 23m" / "last: 2h ago"
        sub-line under the existing ENABLED badge (only shown when
        the timer is enabled and the fields are populated).
      - `formatNextRun()` and `formatLastRun()` helpers convert
        the ISO 8601 timestamps from systemd to friendly relative
        labels. They handle the overdue / just-now edge cases.
  * `app/backup_manager.py`:
      - Fixed `_run_systemctl_user` — same bytes-vs-str bug as
        `schedules._run_systemctl` (passing `(password + "\n").encode()`
        into a `text=True` subprocess call). Changed to a plain str.
      - Fixed `enable_job()` and `disable_job()` to return
        `(ok, err)` tuples instead of bare bools. The
        `api_backups_create` and `api_backups_enable` routes were
        unpacking the return value as a 2-tuple and crashing
        with `TypeError: cannot unpack non-iterable bool object`.
  * Tests: +1 in `tests/test_backup_manager.py` covering the new
    str-stdin contract; the existing `test_enable_job` /
    `test_enable_job_failure` / `test_disable_job` tests were
    updated to the new tuple contract.

### 1.5 — System-services: `mask` / `unmask` actions  [x]`[x]`
AGENTS.md lists `mask` and `unmask` as available actions. The UI
doesn't have buttons for them. Add row actions behind a "..."
overflow menu with confirmation (mask is destructive — user can
unbreak a service by mistake).

Files: `templates/system-services.html`, `app/system_services.py`

**Status:** Done.
  * `templates/system-services.html`:
      - Detail panel: new "Mask" section under the existing
        "Toggle at boot" section, with two labelled buttons
        (Mask in red, Unmask in default style) and a small
        helper text "(prevent any start)".
      - Row actions: added a 4th button next to the existing
        Start/Stop/Restart trio. It toggles between "enable"
        and "disable" depending on the unit's `is_enabled` state
        (icon: check-circle vs circle).
      - `openAuth()`: special-cases the `mask` action with a
        native `confirm()` dialog that explains the consequence
        ("This will link the unit to /dev/null and prevent it
        from being started. You can reverse it later with
        'Unmask'.") before showing the password prompt.
      - The `mask`/`unmask` labels are added to the action
        labels map so the password modal reads "Mask unit" /
        "Unmask unit" instead of the generic "Confirm action".
  * The backend `app/system_services.py` already accepted these
    actions (they're in the `ACTIONS` set), so no backend
    changes were needed. The `api_system_services_action`
    route already handles them via the generic action dispatch.

### 1.6 — Swagger UI link in nav  [x]`[x]`
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

### 2.1 — Plugins page  [x]`[x]`
`GET /api/plugins` returns discovery metadata for loaded plugins.
There's no page that shows them. Add `/plugins` route + template
that lists each plugin's `name`, `version`, and any registered
routes / hooks. Useful even when zero plugins are loaded (shows
the directory and how to add one).

Files: new `templates/plugins.html`, `app/plugins.py`, `server.py`

**Status:** Done.
  * `server.py`: new `/plugins` route that renders
    `templates/plugins.html`. The `/api/plugins` route was
    already there from the existing loader.
  * `templates/plugins.html` (new file):
      - Status cards: Discovered count, Loaded-at-startup count
        (with "(+N failed)" suffix when any plugin errored at
        import), Plugin directory path.
      - Plugin list: each plugin as a card with its name, version
        badge, source file/class, description, and a green
        "loaded" badge. Plugins that failed to import get a red
        "import failed" badge and a hint to check journalctl.
      - Empty state with an icon and the path to drop new
        plugins into.
      - "Writing a plugin" section with a complete example
        showing the PluginBase subclass + register() pattern.
      - "Plugins are NOT auto-reloaded" note (matching the
        docstring in `app/plugins.py`).
  * `templates/index.html`: added a "Plugins" link in the top
    nav, between Files and Import. Purple styling to match
    the page's icon.
  * `app/palette.py`: added a "Plugins" entry so it's
    discoverable via Ctrl+K.

### 2.2 — Config validation page  [x]`[x]`
`app/config_schema.py` has `validate_config()`. No UI exposes it.
Add a "Config" page that calls the validator and renders the
result (validation errors with field paths, or "OK"). Also show
the effective `config.yaml` content (with secrets redacted) in a
read-only viewer.

Files: new `templates/config.html`, `app/config_schema.py`,
`server.py`

**Status:** Done.
  * `server.py`:
      - New `/config` route that renders the page.
      - New `/api/config` GET endpoint that reads config.yaml
        directly, redacts obvious secrets (any field whose name
        contains password/secret/token/key is replaced with
        '***'), and runs `app.config_schema.validate_config`
        to produce an errors list. Pydantic v1 returns
        `errors` as a property, v2 as a method; the handler
        handles both.
      - Soft warning for duplicate command ids (the schema is
        lenient so it doesn't catch this).
  * `templates/config.html` (new file):
      - Status cards: Status (valid/invalid badge), Errors count,
        Warnings count.
      - Validation section: shows the config path, error rows
        with field path and message, soft warnings in yellow.
      - "Effective config (read-only)" section: pretty-printed
        JSON of the redacted config. A note explains the
        redaction policy.
      - "Re-validate" button re-fetches the API (useful after
        the user edits the file on disk and wants to see
        the new state without a hard refresh).
  * `templates/index.html`: added a "Config" link in the top
    nav (blue styling).
  * `app/palette.py`: added a "Configuration" entry so it's
    discoverable via Ctrl+K.
  * Tests: 6 new tests in `tests/test_config.py` covering
    valid / invalid / YAML-parse-error / missing-file /
    secret-redaction / login-required paths.

### 2.3 — Activity log: `user` and `since` filters  [x]`[x]`
Backend supports `?user=...&since=...` but the activity page
only exposes `action`, `target`, `status`. Add the missing filter
inputs.

Files: `templates/activity.html`

**Status:** Done (combined with 5.7 below; same file change).

  * `templates/activity.html`:
      - Filter bar: added a `user` text input and a `since`
        dropdown (Any time / Last hour / Last 24h / Last 7d /
        Last 30d). The dropdown value is converted to a Unix
        timestamp in `loadEntries()` before sending.
      - Table: added a new `User` column (hidden on `md` and
        below to keep mobile usable). The cell shows the
        recorded user, or `—` for anonymous events.
      - Existing `IP` column hidden on `sm` and below (moved
        next to User so the row still fits on narrow screens).
      - All filter inputs share the existing 200ms debounce
        so we don't hammer the API while the user types.

  * The backend (`/api/activity` + `app/activity.list_entries`)
    already accepted these params; no backend changes needed.

---

## Tier 3 — Mobile Responsiveness

### 3.1 — Dashboard header collapses to hamburger on mobile  [x]`[x]`
13+ nav buttons is too many for a mobile top bar. Replace with a
hamburger icon on `<sm` that opens a slide-down menu.

Files: `templates/index.html`

**Status:** Done.
  * `templates/index.html`:
      - New hamburger button (`md:hidden`) before the logo.
      - New `#mobile-menu` div with a 2-column grid of `.nav-tile`
        buttons covering every nav item that was on the desktop
        bar (Monitor, Control, System, Cron, Health, Activity,
        Disk, SSH, Cluster, Files, Plugins, Config, Import,
        Export, Add Service, API Docs, Search, Logout). Add
        Service is highlighted in blue; Logout in red.
      - The desktop wrapper div was renamed to `#nav-bar` and
        given `hidden md:flex` so it disappears on phones.
      - `toggleMobileMenu(force)`: opens/closes the menu,
        swaps the hamburger icon for an X, and refreshes
        lucide icons (lucide only renders icons in visible
        DOM).
      - Auto-close on tile click and on outside click.
      - New `.nav-tile` CSS class with the same look as
        desktop nav pills (border, padding, hover state).

### 3.2 — Touch support for the resizer  [x][x]
The dashboard's drag-resize handle uses mouse events only. Add
`touchstart`/`touchmove`/`touchend` equivalents so the terminal
pane is resizable on tablets.

Files: `templates/index.html`

### 3.3 — Treemap height adapts to viewport  [x][x]
`disk.html` sets the treemap to a fixed 480px. Make it `min(60vh,
480px)` or similar so it fits a phone screen.

Files: `templates/disk.html`

### 3.4 — Force tables to fit small screens  [x][x]
Several pages set a hard `min-width: 920px` on their data tables
(activity, alerts, cron, system-services). Replace with a
"card view" on mobile: each row becomes a stacked card on `<sm`,
restoring the table on `≥sm`.

Files: `templates/activity.html`, `templates/alerts.html`,
`templates/cron.html`, `templates/system-services.html`

### 3.5 — "Show stopped" toggle visible on mobile (docker.html)  [x][x]
Currently `hidden sm:flex` — needs to be reachable on phones.

Files: `templates/docker.html`

---

## Tier 4 — UX Consistency

### 4.1 — Unify modal/confirm/alert patterns  [x]
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

### 4.2 — Loading skeletons  [x][x]
Many pages show a blank table on initial load. Add skeleton rows
(animated grey bars) to activity, alerts, packages, and backup
tables so users see something while the data fetches.

Files: `templates/activity.html`, `templates/alerts.html`,
`templates/packages.html`, `templates/backups.html`

### 4.3 — Debounce filter inputs  [x][x]
`alerts.html`'s service-name filter and the activity page's search
inputs fire on every keystroke. Add 250ms debounce so the API isn't
hammered.

Files: `templates/alerts.html`, `templates/activity.html`

### 4.4 — Unify toast position  [x][x]
`top: 1rem` on most pages, `bottom: 1rem` on cluster/disk/files/ssh.
Pick one (top is the convention).

Files: 4 templates

### 4.5 — Replace `alert()` calls in index.html  [x][x]
Six `alert()` calls in `index.html` (form errors, delete confirm).
Migrate to the toast system.

Files: `templates/index.html`

### 4.6 — Make `login.html` use CSS variables  [x][x]
Currently uses hardcoded Tailwind colors (`bg-gray-950` etc).
Migrate to the shared `:root` variable system so the login page
matches the rest of the app.

Files: `templates/login.html`

### 4.7 — Unify the dashboard header style  [x][x]
`index.html`'s header uses a unique `glass-effect` class.
Bring it in line with the `bg-[var(--bg-card)]/60 backdrop-blur-md`
used everywhere else.

Files: `templates/index.html`

---

## Tier 5 — Polish

### 5.1 — Empty terminal hint  [x][x]
The dashboard's empty terminal pane shows nothing. Add a
"Click + to start a terminal" hint.

Files: `templates/index.html`

`packages.html` only shows pending updates. Add a "Lookup" tab
that takes an arbitrary package name and shows its installed
version and available upgrade (if any).

Files: `templates/packages.html`, `app/package_manager.py`

### 5.3 — Backup restore guide  [x][x]
Backups exist but there's no in-UI hint about where the archive
files live or how to restore them. Add an info banner.

Files: `templates/backups.html`

### 5.4 — Add "List all jobs" to packages page  [x][x]
`GET /api/packages/jobs` returns a list of all install jobs.
Show them in a sidebar so the user can re-open a finished
install's log.

Files: `templates/packages.html`, `app/package_manager.py`

### 5.5 — Make the dependency graph zoomable on touch  [x][x]
The SVG graph in system-services doesn't support pinch-zoom on
mobile. Add `viewBox`-based zoom on double-tap.

Files: `templates/system-services.html`

### 5.6 — Cluster: mDNS results panel  [x][x]
`mdnsBrowse()` only logs results to console. Show them in a
modal with name/host/port badges.

Files: `templates/cluster.html`

### 5.7 — Activity: User column  [x]`[x]`
Backend records `user`; UI doesn't show it. Add the column.

Files: `templates/activity.html`

**Status:** Done (combined with 2.3; same change set). See 2.3
above for the full description. The new column is hidden on
mobile (`hidden md:table-cell`) and the existing `IP` column
was similarly hidden on small screens so the row still fits.

---

## Route-by-route improvement pass

Reviewing and polishing every route one by one, starting with the
dashboard. Each entry records the user-reported issue, the fix, and
how it was verified.

### Route 1 — Dashboard (`/`)  [x]

Files: `templates/index.html` (only file changed; no backend changes).

1. **Command palette icon.** The toolbar button used a generic
   `search` magnifier, identical to filter inputs elsewhere.
   Changed to the `command` (⌘) icon, the standard command-palette
   glyph. The `⌘K` kbd badge rendered as odd-looking text, so it
   was replaced with a plain `Command + K` text label (tooltip
   still reads "Command palette (Ctrl+K)").

2. **Terminal maximize.** Added a maximize/restore button
   (`maximize-2` / `minimize-2` icon) in the terminal header next
   to the `+` button. Maximizing hides the services pane and grows
   the terminal to the full content height; restoring returns the
   previous height. Manually dragging while maximized exits
   maximize mode cleanly (pane visibility is restored).

3. **Draggable to full page.** The divider previously stopped well
   short of the top. Three stacked causes were found and fixed:
   - The drag cap only allowed `container − 150px` (later `−48px`,
     then `−32px`); it now allows the full height.
   - `#services-pane` carries a `min-h-[250px]` class, so flexbox
     clamped it during a plain drag and the divider hit a ceiling.
     The min-height is now relaxed inline while dragging and
     restored on release when the terminal is back to normal size.
   - **Root cause (found via live browser measurement):** the
     terminal pane had default `flex-shrink: 1`, so once the
     services pane hit its content floor (~48px), flexbox squished
     the terminal back no matter what height was set. The pane is
     now `shrink-0`, and past the services floor the pane is
     hidden (`display: none`) so the terminal takes everything
     except the 12px divider bar — which stays visible so the user
     can always drag back down.
   - Terminal height changes (drag / maximize / restore) re-fit
     xterm through one shared `_fitActiveTerminal()` helper
     (double-rAF so layout settles first) instead of three
     duplicated fit blocks.

4. **Cropped width on new terminals.** `term.open()` ran while the
   new tab's container was still `hidden`, so xterm measured zero
   width; the single-rAF fit in `switchTab()` could fire before
   layout settled, leaving the terminal narrow until the next
   manual resize. `switchTab()` now fits in a double-rAF plus a
   150ms fallback re-fit, re-emitting `terminal_resize` each time.

**Verified live** (Playwright against the running server, plus
`pytest`: 799 passed):
- Simulated drag to top: terminal 593/605px = 98% coverage,
  services pane hidden; drag back down restores both panes.
- Maximize → 593px / hidden; restore → previous sizes back.
- Scrollbar check: with 30 extra cards injected, the services
  pane measured `scrollHeight 1672px` vs visible `373px` with
  `overflow-y: auto` — a vertical scrollbar appears on the right
  side of the service panel; header, toolbar, divider, and
  terminal stay fixed. (Test cards removed afterwards.)

**Known remaining sliver:** the 12px divider bar always stays
visible at full height (it is the drag-back handle). Removing it
would require button-only restore; left as-is by design.

### Route 2 — Monitor (`/monitor`)  [x]

Files: `templates/monitor.html` (only file changed; no backend changes).

1. **Content width.** The `<main>` wrapper was `max-w-7xl`
   (1280px), wasting ~35% on wide screens. Now `w-[90%]` with a
   `max-w-[1760px]` cap. Measured live: 1152px on a 1280px
   viewport = exactly 90%.

2. **Chart x-axis no longer rescales (CPU, memory, network).**
   All history arrays started empty and grew point-by-point, so
   Chart.js stretched the time axis until 60 points arrived.
   Every history array (`timeLabels`, `cpuAvgHistory`,
   `memHistory`, `netRecvHistory`, `netSentHistory`, plus each
   per-core array at CPU-chart init) is now pre-filled to the
   full 60-point window with nulls; new values enter from the
   right via the existing push+shift. `spanGaps: false` is forced
   on every dataset in `makeChartConfig()`, so the line stays
   broken across the leading nulls — a fresh page shows an empty
   plot that fills rightward, and the x scale stays pinned at
   0–59 forever.

3. **Top Processes fixed column widths.** The table is now
   `table-fixed` with a `<colgroup>` (PID 72px, Name flexible,
   CPU 92px, MEM 92px, State 128px); cells truncate instead of
   pushing columns around. Measured live across two data
   refreshes 4s apart: widths identical (72/676/92/92/128).

**Verified live** (Playwright against the running server):
- Width ratio 90%, `table-layout: fixed`, columns stable.
- After ~20s: `labelsLen 60`, mem/net data length 60 with 11
  real points, x scale `min 0 / max 59`.

4. **Draggable column widths.** Each of the 5 header cells has a
   `col-resize` grip on its right edge (blue highlight on hover);
   dragging adjusts the `<colgroup>` width (min 56px) and the
   choice persists in `localStorage` (`ssm-proc-colwidths`) across
   reloads. Verified live: Name column dragged 676→776px, prefs
   saved as `[72,776,92,92,128]`.

5. **Pause / Resume live processes.** A Pause button in the Top
   Processes header freezes table updates (charts and bars keep
   updating) so rows can be selected and copied; it turns yellow
   and reads Resume while paused. Verified live: table HTML
   byte-identical across 4.5s while paused, updating again after
   Resume.

### Route 3 — Control (`/control`)  [x]

Files: `templates/control.html` (only file changed; no backend changes).

1. **Content width.** Same `max-w-7xl` narrowness as the monitor
   had; now `w-[90%]` capped at 1760px. Measured live: 90%.

2. **Escape never closed the confirm modal.** The keydown handler
   checked for absence of a `hidden` class, but both modals
   toggle `.active` (and never carry `hidden`), so Escape always
   took the auth branch — a no-op when auth was shut — and
   returned before reaching the confirm branch. Now checks
   `.active`, and as a bonus Escape also closes the output
   drawer when no modal is open.

3. **Explicit click event.** `handleCommand()` read the implicit
   global `event`, which breaks in strict contexts and on
   programmatic calls. Both call sites now pass `event`
   explicitly (`handleCommand(event, …)`); `executeConfirmed()`
   passes `null`, and the button lookup guards against it
   (`setExecuting` already no-ops on null).

**Verified live** (Playwright against the running server):
- 11 command tiles render; width ratio 90%.
- Reboot → confirm opens → Escape closes it.
- Disk Usage click → spinner runs → output drawer opens with
  real command output → spinner clears (explicit-event path).
- Escape closes the output drawer.

### Route 4 — System Services (`/system-services`)  [x]

Files: `templates/system-services.html` (only file changed; no
backend changes).

1. **Content width.** Same narrow `max-w-7xl` wrapper; now
   `w-[90%]` capped at 1760px like monitor/control. Measured
   live: 90%.

2. **Failed detail fetch slammed the panel shut.** Ghost entries
   such as `-.mount` appear in `list-units` (476 rows here) but
   `systemctl show` reports them not-found, so the detail API
   404s. Clicking one flashed an error toast and instantly closed
   the panel. `openPanel()` now calls a new `showPanelError()`
   instead: the panel stays open on the Overview tab with a red
   error badge, the message as the description, all action
   buttons disabled, and a Retry button in the metadata grid
   (the toast is kept for visibility).

**Verified live** (Playwright against the running server):
- cron.service row click → panel opens with the real
  description ("Regular background program processing da…").
- `-.mount` → panel stays open showing "Unit not found" with a
  working Retry button (retry keeps the panel open).

3. **Name column too wide, Actions needed horizontal scroll.**
   Measured live: the table overflowed by 8px (`scrollWidth`
   1110 vs 1102). Per-cell measurement pinned it down — not the
   Name column itself, but the Actions cell content (4 icon
   buttons) overflowing its 130px column by exactly 8px. Fixed
   with two changes: Name `32%` → `26%` (353→287px, still
   truncates long names with a tooltip) and Actions `130px` →
   `142px`. Verified: no horizontal scrollbar, last action
   button's right edge inside the visible area.

4. **Table height capped, wasting page space.** `.table-scroll`
   had `max-height: min(560px, calc(100dvh - 250px))`, so on tall
   screens the table stopped at 560px with empty space below.
   Dropped the 560px cap → `max-height: calc(100dvh - 250px)`,
   so the scrollable table region extends to near the bottom of
   any viewport (min-height 240px kept for short/filtered
   lists).

5. **Auto-refresh stole row clicks; refresh button removed.**
   The 5s re-render could replace rows mid-click. Added a
   Live/Pause toggle (pause icon → play, yellow "Paused" state)
   that skips the refresh tick; the manual Refresh button was
   removed since refresh is automatic when live. Verified: rows
   byte-identical across 6s while paused, updating on resume.

6. **Bulk actions.** Checkbox column (plus select-all header
   checkbox that tracks visible rows); selection survives
   re-renders via a name set. A purple bulk bar ("N selected")
   offers Start / Stop / Restart / Enable / Disable / Clear.
   The auth modal was extended with a bulk path that runs the
   action per unit with one password, then toasts a summary
   ("restart: 3 ok, 1 failed (foo.service)"). Verified: 476
   checkboxes, "2 selected", modal reads "Start units / start 2
   selected unit(s)".

7. **Running / Static filters.** Tabs existed only for
   All/Active/Inactive/Failed although badges show running and
   static. Added Running and Static tabs (client-side filters on
   `is_running` / `unit_file_state`; the server is asked for all
   units in those modes). Verified: Running "72 / 476", Static
   "123 / 476".

8. **Description search.** The search only matched unit names;
   "kernel" matched nothing useful. It now matches name OR
   description. Verified: "kernel" → "16 / 476 units".

9. **Stale side panel.** Overview data never refreshed after
   opening. Each list tick now silently re-fetches the open
   unit — but only on the Overview tab, never on Logs/Graph/
   Unit-file, and never while the auth/edit modal is open.
   Verified: panel stays open and populated across ticks.

10. **Logs tab filter.** Added a "Filter lines…" input beside
    the Reload button (mirrors `/logs`): non-matching lines
    hide, matches highlight with `<mark>`, counter reads "31 of
    100 lines", debounced 150ms, applies to streamed lines too.
    Verified: no-match hides all 100; "session opened" finds 31
    with highlights.

11. **Graph legend.** Static legend under the hint (Requires
    solid blue, Wants dashed purple, After/Before dotted gray).

12. **Failed-attention chip.** Red "N failed" chip appears in
    the filter bar when any unit is failed; clicking jumps to
    the Failed filter. (Hidden on this host — 0 failed units —
    so chip visibility itself was verified by code path; the
    Failed tab shows "0 units".)

### Route 5 — Docker (`/docker`)  [x]

Files: `templates/docker.html` (only file changed; no backend
changes).

1. **Content width.** Same narrow `max-w-7xl` wrapper; now
   `w-[90%]` capped at 1760px. Measured live: 90%.

2. **Detail tabs cleared the view-switcher highlight (real
   bug).** `switchTab()` ran `document.querySelectorAll(
   '.tab-button')`, which also matches the subheader
   Containers/Images switcher (same class, no `data-tab`), so
   clicking Details/Stats/Logs un-highlighted the Containers
   tab. Scoped to `#detail-panel [data-tab]`. Verified live
   with 59 containers: `containers:true` survives Stats and
   Logs tab switches.

3. **Table height cap.** `.table-wrap` had `max-height:
   min(520px, …)`; dropped the 520px cap so the list extends to
   near the viewport bottom like system-services.

4. **Stats crash on stopped containers.** `renderStats()` called
   `.toFixed(1)` directly on `cpu_percent`/`memory_percent`,
   which can be null for stopped containers — throwing inside
   the poll callback and silencing further updates. Both are
   now coerced (`Number(x) || 0`). Verified live against a
   real stopped container: bars render, no errors.

5. **Unbounded log DOM.** The Logs tab appended streamed lines
   forever. Now trims to 5000 lines like the system-services
   viewer.

**Verified live** (Playwright against the running server, 59
containers): width 90%, view-tab highlight stable, stats bars
render for running and stopped containers, zero JS errors.

6. **Table squeezed by the phantom detail track.** With the
   detail panel closed, the table still rendered at ~64% width
   (measured 704/1102px, 400px wasted). Cause: the grid kept
   `lg:grid-cols-[1fr_24rem]` even when the aside was
   `display:none` — the empty 24rem track still reserves space.
   The two-column layout now applies via a `.with-detail` class
   only while the panel is open. Verified: 1104/1104px (100%)
   closed, clean 704+384+16 split open, full width restored on
   close.

### Route 5b — Docker round 2: fixes + stacks/daemon/prune  [x]

Backend: `app/docker_manager.py` gained `daemon_info()` (subset
of `docker info`), `prune_images()` (`images.prune()` → deleted
count + reclaimed bytes), and `restart_daemon(password)` (`sudo
systemctl restart docker` with the app password piped to
`sudo -S`, same pattern as systemd unit control). Routes in
`server.py`: `GET /api/docker/daemon`, `POST
/api/docker/daemon/restart`, `POST /api/docker/images/prune`
(all with activity logging). Tests: +7 in
`tests/test_docker_manager.py` (info subset/error, prune
ok/error, restart requires-password/ok-sudo-argv/bad-password).

Frontend (`templates/docker.html` only):

1. **Pause-live.** Refresh button replaced with a Live/Pause
   toggle; the 15s tick is skipped while paused.
2. **Bulk actions.** Checkbox column + select-all + "N selected"
   bar (Start/Stop/Restart) with `uiConfirm` and a per-container
   summary toast. Selection survives re-renders.
3. **Detail panel refresh.** Each tick re-fetches the open
   container, but only on the Details tab (never disturbs Logs
   streaming or Stats polling).
4. **Stats trend chart.** Chart.js (same CDN/pinning as
   `/monitor`) + 60-point null-prefilled CPU/memory histories
   with `spanGaps:false`; window restarts empty per container.
5. **Images prune.** "X reclaimable (N dangling)" line +
   Prune button (`uiConfirm` → POST → toast with reclaimed
   bytes → reload).
6. **Clickable ports.** Published tcp ports render as
   `http://host:port` links (current hostname, new tab);
   unpublished/other formats stay plain text.
7. **State filter tabs.** All / Running / Stopped
   (exited+dead+created) / Paused / Restarting, client-side.
8. **Remove modal shows the name** ("pensive_rubin") instead of
   the 64-char ID hash.
9. **Daemon view.** New tab: Version / Containers / Images /
   Storage cards + OS/kernel/arch/CPUs/memory/paused details +
   Restart Docker button (`uiConfirm` → `uiPromptPassword` →
   POST → reconnect poll after 5s).
10. **Stacks view.** New tab grouping by `compose_project`
    (+ a standalone group): per-stack running x/y counts,
    container rows with state, Start/Stop/Restart-all buttons
    with confirm and per-stack summary toast. Reworked into a
    full-width accordion list (one compact header row per
    stack: chevron, status dot, name, x/y running, action
    buttons) instead of a card grid — 18 stacks scan in one
    screen. Clicking a header expands its containers (name,
    image, state badge, status); clicking a container jumps to
    its detail in the Containers view. Expand/Collapse-all
    buttons in the header. All stacks start collapsed (an
    earlier auto-expand of degraded stacks was removed as
    inconsistent); a "Filter stacks or containers..." search
    matches stack names as well as container names/images,
    showing only matching containers inside a hit stack. The
    stacks list reuses the containers table's `.table-wrap`
    scroll container, so both lists share the same max-height
    and scroll behavior (verified identical computed styles).

**Verified live** (Playwright, 59 containers, 52 images, zero
JS errors): Running filter "53 / 59", bulk "2 selected", 24
port links, trend chart visible and accumulating, 18 stacks,
prune line "2.0 GB reclaimable (4 dangling)", daemon cards
with real host data. Bulk/stack/prune/restart *execution*
was intentionally not fired on the live host; code paths
mirror already-tested single-action calls. (One syntax slip
— a dropped `});` in `loadImages` — was caught by the
console-error check and fixed before verification.)

### Route 6 — Cron (`/cron`)  [x]

Files: `templates/cron.html` (only file changed; no backend
changes).

1. **Content width.** Same narrow `max-w-7xl` wrapper; now
   `w-[90%]` capped at 1760px. Measured live: 90%, table fits
   with no horizontal scroll (9 jobs).

2. **Table height cap.** `.table-scroll` had `max-height:
   min(560px, …)`; dropped the 560px cap so the list extends to
   near the viewport bottom.

3. **Enable/disable toggle was inverted (real bug).** The
   `change` event fires AFTER the checkbox flips, so
   `cb.checked` is already the desired end state — but the code
   read `!cb.checked` as the target. Unchecking an enabled job
   opened an "Enable cron job" dialog and sent `enabled: true`
   to the server (title, payload, and toast all backwards).
   Now uses `cb.checked` as `wantEnabled`. Verified live:
   uncheck → "Disable cron job" modal with the visual
   reverted; no jobs are disabled on this host so the reverse
   direction was verified by code symmetry (single boolean
   path). Nothing was executed (modal cancelled).

**Verified live** (Playwright, zero JS errors, `node --check`
clean).

### Route 6b — Cron round 2: add jobs, next runs, bulk  [x]

Backend (`app/cron_manager.py`): `next_run(expr, now)` —
next run as ISO string via day scan (≤ ~4y so Feb 29 works,
standard dom/dow OR semantics, 7 = Sunday, bare `5/10` steps;
~1ms worst case) — and `add_job(filename, schedule, user,
command, password)` — validates everything, appends to
`/etc/cron.d/<name>` via the same sudo-cp-tempfile pattern as
toggles. Routes: `POST /api/cron/jobs` (+ activity log);
`GET /api/cron` now includes `next_run` per enabled job.
Tests: +18 in `tests/test_cron_manager.py` (next-run cases,
add validation/success, route validation test).

Frontend (`templates/cron.html` only):

1. **Add-job modal** ("Add job" button): file (default
   `manager`), schedule with live `/api/cron/validate`
   feedback, user (default `root`), command, sudo password.
   Creates via POST, toasts, reloads.
2. **Next-run column** (fixed 130px): relative labels ("in
   19m", "in 5h 27m", "paused" for disabled, "—" when
   unknown) with exact ISO in the tooltip.
3. **Row expander**: clicking a row shows the raw line +
   source; per-user rows get a `crontab -e` hint since the UI
   can't edit them.
4. **Bulk enable/disable**: checkbox column + select-all +
   "N selected" bar; one password prompt loops the toggle
   endpoint with an ok/failed summary toast.
5. **Validator link**: a "New job" button appears next to a
   valid expression and opens the Add dialog prefilled.

Validation: reviewed with `agy` (read mode) before commit —
it caught 4 blocking issues, all fixed: (a) unreadable
existing cron.d files no longer truncate to a blank header
(hard error instead — data-loss fix); (b) dots banned from
file names (Debian/Ubuntu cron ignores dotted
`/etc/cron.d/` files); (c) "App password" relabeled "Sudo
password"; (d) bulk checkbox `onchange` uses
`this.dataset.key` instead of interpolated quoting. Also
applied its suggestions: dow `7` accepted, bare `5/10`
expansion, tempfile kept at 0600, plus new-file/Feb-29/route
tests. Verified live: dotted name → invalid, dow-7 valid,
next_run in list payload, "New job" button behavior, zero JS
errors.

### Next route: (to be picked — `/cron` done)

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
