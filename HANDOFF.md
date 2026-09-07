# Agent Handoff — server-services-manager

This document describes the current state of the
`server-services-manager` repo and what the next agent should
pick up.

---

## TL;DR

The app is a Flask + SocketIO process manager / dashboard for
running user-defined services. It's running at
`http://localhost:8881` under systemd as
`server-services-manager.service`. Login is single-password
(`PASSWORD` env var, defaults to `admin`).

**Current branch:** `development`
**Last commit:** `5e99596 fix(nav): hamburger breakpoint fix + Pill + More desktop nav`
**Tests:** 741 passing (`python -m pytest tests/ -q`)
**Python:** 3.13, eventlet, Flask, Flask-SocketIO, pydantic, psutil
**Repo state:** clean working tree, all changes committed

---

## Recent work (already done)

### 1. UI improvements roadmap (UI-IMPROVEMENTS.md)

A roadmap was created at `UI-IMPROVEMENTS.md` with 28 items in 5
tiers. **11 of 28 are complete.** All Tier 1 (high-priority
backend gaps) and Tier 2 (missing feature pages) are done.

Done items:
- 1.1: Listen for `service_event` on dashboard → state-change toasts
- 1.2: Schedule "Run Now" button + timer status
- 1.3: Docker Images tab (browse + remove)
- 1.4: Backup timer status (next_run / last_run)
- 1.5: System-services mask/unmask actions
- 1.6: Swagger UI link in nav (`/docs/`)
- 2.1: Plugins page (`/plugins`)
- 2.2: Config validation page (`/config`)
- 2.3 + 5.7: Activity log user/since filters + User column
- 3.1: Mobile hamburger menu

### 2. Auto-start toggle fix (latest commit)

The "Auto-start on launch" toggle in the Add Service modal
rendered as a 2px-wide sliver because the `<label>` defaulted
to `display: inline`, so Tailwind's `w-10 h-5` sizing was
ignored. Fixed by wrapping the label in a `relative inline-block
w-10 h-5 flex-shrink-0` parent and using `absolute inset-0` on
the label. Now the 40x20 track renders correctly and the
white toggle ball is visible.

### 3. Unified top nav (latest commit)

The dashboard had a custom inline nav with 18+ buttons; every
other page used a stripped-down header macro with just a back
arrow, title, and page-specific actions. The fix:

- Extended `templates/_macros.html` `header()` macro to render a
  full 19-page global nav automatically. Current page gets a
  blue background.
- Every page (including the dashboard) now passes
  `current='/<path>'` to the macro.
- The dashboard's `index.html` was rewritten to use the macro
  (passes its own actions through: Import, Export, Add Service,
  API, Logout, Search, Connection status).
- The `<style>` and `<script>` blocks needed for the shared nav
  CSS and the mobile-menu toggle live INSIDE the macro (Jinja's
  macro-scope rules mean a template-level `{% set %}` isn't
  visible inside a `{% macro %}` body). Wrapped in `{% raw %}`
  so the JS `{{ }}` doesn't trigger Jinja's lexer.
- Removed the old custom mobile menu HTML and the
  `toggleMobileMenu()` function from `index.html` — the macro
  provides `window.uiToggleMobileMenu()`.

The result: every page shows the same horizontal nav bar with
all 19 pages. On mobile (<lg breakpoint), it collapses to a
hamburger menu that opens a 3-column grid of all the same
tiles, mirroring the desktop nav exactly. The Docker tab is
now visible in the nav (it was missing before).

### 4. Docker table column widths (commit `02cb138`)

The Actions column in the Docker containers table was capped at
4.5rem (72px) but holds three 28px row-action buttons (start,
stop, restart). The restart button existed in the code but was
clipped by the narrow column. Fixed by widening to 7rem (112px)
and rebalancing the other columns (Name 30→28%, Image 25→22%,
State 18→16%, Ports 18→16%).

### 5. Nav simplification + hamburger fix (commit `5e99596`)

**Hamburger fix:**
- Changed breakpoint from `lg:hidden` to `xl:hidden` so the
  hamburger stays visible on screens 1024-1280px (was hidden
  while the mobile menu was still shown).
- Added `onTouchStart` fallback so touch events trigger the
  toggle on mobile.

**"Pill + More" desktop nav:**
- The 19-item nav was too crowded and wrapped to 2-3 rows on
  most laptops. Now shows only 7 primary items as pills:
  Dashboard, Monitor, System, Docker, SSH, Files, Activity.
- The remaining 12 items (Control, Cron, Health, Disk, Cluster,
  Plugins, Config, Alerts, Packages, Backups, Firewall, Logs)
  are grouped under a "More ▼" dropdown.
- API Docs is in the dropdown too (was previously only in the
  mobile menu).
- The dropdown opens/closes on click, the chevron rotates, and
  clicking outside closes it. Active pages inside the dropdown
  are highlighted when opened.

### 6. Bonus fixes while in the area

- `app/schedules.py:_run_systemctl` was passing bytes into a
  `text=True` subprocess call → fixed to str.
- `app/backup_manager.py:_run_systemctl_user` had the same bug →
  fixed.
- `app/backup_manager.py:enable_job` / `disable_job` were
  returning bare `bool` but the routes unpacked them as
  `(ok, err)` tuples → fixed to return tuples.
- `app/docker_manager.py:list_images` was passing the RFC 3339
  `Created` string straight to the JS, which expected a Unix
  timestamp → fixed by parsing to a float on the server side.
- Added `.playwright-mcp/` to `.gitignore` so debug screenshots
  don't leak.

---

## Current state of the app

### File layout
```
server.py                      # Flask app, ~3200 lines, all routes
app/
  process_manager.py            # Programs + ProcessManager
  terminal_manager.py          # xterm.js PTY sessions
  system_services.py           # systemd unit introspection
  log_streamer.py              # journalctl -f SSE
  cron_manager.py              # /etc/crontab + /etc/cron.d
  docker_manager.py            # Docker SDK wrapper
  alert_log.py                 # notification delivery log
  package_manager.py           # apt/dnf
  firewall_manager.py          # ufw/firewalld
  backup_manager.py            # systemd-timer-based backups
  cluster_manager.py            # multi-host peer registry
  disk_manager.py               # chroot-safe du
  file_explorer.py             # full file manager
  ssh_manager.py               # authorized_keys
  health.py                    # HealthMonitor background thread
  notifier.py                  # ntfy/webhook/telegram/email
  activity.py                  # SQLite audit log
  palette.py                   # Ctrl+K command palette
  schedules.py                 # systemd-timer cron jobs
  resource_limits.py           # systemd drop-in limits
  log_persistence.py           # per-program log files
  config_schema.py             # pydantic validation
  plugins.py                   # drop-in PluginBase loader
  openapi.py                   # OpenAPI 3.0 spec generator
templates/
  _macros.html                 # SHARED header macro
  index.html                   # dashboard (uses macro now)
  monitor.html, control.html, system-services.html, cron.html,
  notifications.html, activity.html, alerts.html, packages.html,
  logs.html, firewall.html, backups.html, disk.html, ssh.html,
  cluster.html, files.html, docker.html, plugins.html, config.html
  login.html
config.yaml                    # programs: [] (gitignored)
AGENTS.md                      # project guide for AI agents
ROADMAP.md                     # historical roadmap
UI-IMPROVEMENTS.md             # current UI improvements backlog
```

### Pages (with routes)
| Route | Page | Template |
|-------|------|----------|
| `/` | Dashboard | `index.html` |
| `/login`, `/logout` | Login | `login.html` |
| `/monitor` | System monitor | `monitor.html` |
| `/control` | Control panel | `control.html` |
| `/system-services` | systemd units | `system-services.html` |
| `/cron` | Cron jobs | `cron.html` |
| `/notifications` | Health/notifications | `notifications.html` |
| `/alerts` | Notification log | `alerts.html` |
| `/activity` | Audit log | `activity.html` |
| `/packages` | Package updates | `packages.html` |
| `/logs` | Log search | `logs.html` |
| `/firewall` | Firewall | `firewall.html` |
| `/backups` | Backups | `backups.html` |
| `/disk` | Disk usage | `disk.html` |
| `/ssh` | SSH keys | `ssh.html` |
| `/files` | File explorer | `files.html` |
| `/cluster` | Multi-host | `cluster.html` |
| `/docker` | Docker | `docker.html` |
| `/plugins` | User plugins | `plugins.html` |
| `/config` | Config validation | `config.html` |
| `/health` | Health check | (returns JSON) |
| `/docs/`, `/openapi.json` | Swagger UI | (inline) |

All pages use `{{ ui.header(...) }}` from `_macros.html` with
`current='/<route>'` to mark the active nav pill.

### Running the app

```bash
# Start/stop:
systemctl --user start|stop|restart server-services-manager

# Tail logs:
journalctl --user -u server-services-manager -f

# Run tests:
python -m pytest tests/ -q

# Manual launch (without systemd):
./start.sh   # uses config.yaml from project root
```

The app uses `eventlet.monkey_patch()` at the top of `server.py`.
If you see the Eventlet deprecation warning, it's harmless.

There's a known issue: when restarting, sometimes a second copy
of the process stays running and the browser sees stale content.
Fix: `ps aux | grep server-services` and `kill -9 <old_pid>`.

---

## What's left to do (UI-IMPROVEMENTS.md)

17 items remain, mostly Tier 3 (mobile polish) and Tier 4
(UX consistency). Full list in `UI-IMPROVEMENTS.md` but here's
the short list:

**Tier 3 — Mobile responsiveness (medium priority):**
- 3.2: Touch support for resizer (the resizer between services
  pane and terminal pane only listens to mousedown, not
  touchstart)
- 3.3: Treemap height adapts to viewport (currently fixed 480px
  in `disk.html` line 148; should be `min(60vh, 480px)` or
  similar)
- 3.4: Tables to card view on mobile (activity, alerts, cron,
  system-services all set `min-width: 920px` on their data
  tables; should collapse to a card list on `<sm`)
- 3.5: Show stopped toggle on mobile (docker.html line 259
  `hidden sm:flex` hides it on mobile)

**Tier 4 — UX consistency (low priority):**
- 4.1: Unify modal/confirm/alert patterns (3 patterns in use:
  `hidden` class toggle, `.active` class, `.open` class;
  also `confirm()` / `alert()` / `prompt()` for destructive
  actions)
- 4.2: Loading skeletons (many pages show blank table on initial
  load)
- 4.3: Debounce filter inputs (alerts.html fires API calls on
  every keystroke)
- 4.4: Unify toast position (top vs bottom is inconsistent)
- 4.5: Replace `alert()` calls in `index.html` (6 of them at
  lines 897, 901, 919, 928, 1170, 1248) with the new toast()
  helper from item 1.1
- 4.6: `login.html` use CSS variables (currently uses hardcoded
  Tailwind colors instead of the shared `:root` vars)
- 4.7: Unify dashboard header style (uses a unique
  `glass-effect` class; the macro uses `bg-[var(--bg-card)]/60
  backdrop-blur-md`)

**Tier 5 — Polish (low priority):**
- 5.1: Empty terminal hint ("Click + to start a terminal")
- 5.2: Package search/lookup
- 5.3: Backup restore guide
- 5.4: List all install jobs in packages page
- 5.5: Zoomable dependency graph on touch (system-services page)
- 5.6: Cluster mDNS results panel (currently `console.log`d)

For each item, the markdown contains the rationale, plan, and
commit history. The format is: `[x]` for done, `[ ]` for pending.

---

## Conventions / things to know

### Coding style
- Backend uses eventlet + threads for async. Don't use async/await
  in Flask routes.
- Subprocess calls: `text=True` requires str stdin, not bytes
  (we hit this bug twice — see commits `cda5a0f` and `ae48861`).
- Functions returning success/failure tuples use `(ok, err)`
  where `err` is a string. Bare bools break route handlers that
  try to unpack.
- All API routes require login except `/health` and `/login`.
  The `before_request` hook enforces this.

### Frontend style
- Tailwind classes with custom CSS vars (e.g. `var(--accent-blue)`)
  for theme colors.
- Lucide icons via `<i data-lucide="icon-name">` (resolved by
  `lucide.createIcons()`).
- xterm.js for terminals.
- Chart.js for monitor charts.
- No build step. CSS is in `<style>` blocks inside each page.

### Templates
- `_macros.html` provides the shared `header()` macro.
- Pages call it like:
  ```jinja
  {% import "_macros.html" as ui %}
  {% set actions %}<button>...</button>{% endset %}
  {{ ui.header(
      title="Page Title",
      icon="lucide-name",
      icon_color="var(--accent-blue)",
      actions=actions,
      current='/route',
  ) }}
  ```
- The `current` param marks the active nav pill.

### Secret-leak guard
- `tools/secrets.txt` is gitignored (user-specific patterns).
- `tools/check-secrets.py` and `tools/clean-history.sh` are the
  guard tools. **Do not commit real secrets, home directory paths,
  program names, or any identifying information.**

### Tests
- `python -m pytest tests/ -q` runs everything.
- New tests go alongside the relevant module:
  `tests/test_<module>.py`.
- Mock subprocess calls; don't hit real systemctl/docker/etc.
- `tests/conftest.py` provides `temp_config` and
  `process_manager` fixtures.

---

## Debug tips

- If the browser shows stale content, kill the old process:
  `ps aux | grep server-services; kill -9 <old_pid>; systemctl --user restart server-services-manager`
- `journalctl --user -u server-services-manager -f` for live logs.
- The Playwright MCP is set up; debug screenshots go to
  `.playwright-mcp/` (gitignored).
- `templates/_macros.html` is the single source of truth for the
  top nav. Changes there affect every page.
- For test clients (Flask `test_client()`), Jinja's `app.jinja_env`
  is shared with the live server, so the same auto-escape
  behavior applies. Use `{{ html|safe }}` for trusted HTML
  strings (otherwise `<style>` and `<script>` tags get
  HTML-encoded).

---

## Suggested next steps for the new agent

1. **Read `UI-IMPROVEMENTS.md`** for the full backlog. It has
   ~85 lines of context per item.
2. **Pick a Tier 3 or 4 item** to start. Tier 3 (mobile) is more
   impactful; Tier 4 (UX consistency) is easier.
3. **Mark the item `[~]` in `UI-IMPROVEMENTS.md`** when you start
   and `[x]` when done.
4. **Run `python -m pytest tests/ -q`** before committing.
5. **Commit each item separately** with a descriptive message.
6. **Check `git status` for personal info** before committing
   (the secret-leak guard is `tools/check-secrets.py` — review
   your diff for any home paths or passwords).
7. **Use Playwright MCP** to visually verify UI changes (use
   `resize` for mobile, navigate to each page, screenshot).
8. **Restart the server** (`systemctl --user restart ...`)
   after template changes; `TEMPLATES_AUTO_RELOAD=True` is set
   but Jinja can still cache the parsed template in-process.
9. **Known gotcha**: Playwright's `click()` on the hamburger
   button (`#ui-menu-btn`) doesn't work via the MCP abstraction
   due to a Playwright + Lucide SVG interaction, but real user
   taps work fine. Use `page.evaluate('document.getElementById(
   "ui-menu-btn").dispatchEvent(new MouseEvent("click",
   {bubbles:true}))')` to programmatically test the mobile menu
   toggle.
10. **"More" dropdown active indicator**: When on a page inside
   the "More" dropdown, the "More" pill itself doesn't show an
   active state. The active item IS highlighted inside the
   dropdown once opened. Consider adding the `active` class to
   the "More" pill when `current` matches any of its 12 routes
   (requires passing the full route list to the header macro).

The app is stable, the tests pass, and all 3 reported issues
are fixed. Good luck.
