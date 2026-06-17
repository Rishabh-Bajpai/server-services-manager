# Server Services Manager — Roadmap

Consolidated future development plan, combining the original
IMPLEMENTATION_PLAN phases (0–17, mostly delivered) with new features
requested after the initial round of work.

---

## Legend

| Icon | Meaning |
|------|---------|
| ✅ | Delivered |
| 🔜 | Next up |
| 💡 | Proposed |
| ❌ | Deferred |

---

## Phase 0 — Foundation (✅ Delivered)

Basic app structure: dashboard, process manager, system monitor,
terminal, file manager, control panel, auth, responsive UI.

---

## Phases 1–17 (✅ Delivered)

| # | Feature | Status |
|---|---------|--------|
| 1 | System services page (browse + control systemd units) | ✅ |
| 2 | Live log streaming via SSE (`journalctl -f`) | ✅ |
| 3 | Cron job management (inspect + toggle `/etc/crontab` & `/etc/cron.d/*`) | ✅ |
| 4 | Health checks + notifications (HTTP/TCP/cmd, ntfy/webhook/telegram/email) | ✅ |
| 5 | Dependencies graph (BFS, SVG) | ✅ |
| 6 | Autostart on boot (`systemctl enable` toggle) | ✅ |
| 7 | Activity log (SQLite, filtered view, CSV export) | ✅ |
| 8 | Command palette (Ctrl+K, fuzzy search) | ✅ |
| 9 | Scheduled tasks (systemd timers, cron-style OnCalendar) | ✅ |
| 10 | Backup / restore (JSON export/import) | ✅ |
| 11 | Resource limits (CPUQuota, MemoryMax, etc. via systemd drop-in) | ✅ |
| 12 | Webhooks on state changes (program start/stop/fail → notifier fanout) | ✅ |
| 13 | Config schema validation (pydantic, lenient, extra=allow) | ✅ |
| 14 | Plugin system (drop-in Python modules, PluginBase ABC) | ✅ |
| 15 | Per-service log persistence (rotated at 512 KB, survives restarts) | ✅ |
| — | Secret-leak guardrails (`tools/check-secrets.py`, `tools/scrub.py`, `tools/clean-history.sh`) | ✅ |

---

## Phase 18 — Docker Container Management (🔜 Next)

> **User says:** "List, start/stop, logs, stats, exec. Natural sibling of
> the systemd UI — same CRUD + log streaming pattern."

- New `app/docker_manager.py` — wraps Python `docker` SDK
- List containers (running/stopped/all), start/stop/restart
- Stream logs (reuse SSE pattern from `app/log_streamer.py`)
- Container stats (CPU, memory, network) → frontend charts
- Exec into a container (PTY terminal, reuse `app/terminal_manager.py`)
- New `/docker` page with containers table + side panel (details/logs/stats/exec)
- `docker-compose` support (list projects, up/down/logs)
- **Missing-socket UX:** page detects absent/unreadable `/var/run/docker.sock`
  and renders a hint card with the exact `sudo usermod -aG docker $USER`
  command, rather than crashing. The page is reachable even without Docker.
- **Reuses:** `templates/system-services.html` pattern, log streamer, terminal manager
- **Tests:** mocked docker SDK calls, container lifecycle, log parsing

---

## Phase 19 — Alert History / Notification Log (🔜 Next)

> **User says:** "Health checks already fire events, but there's no
> persistent record of *when* ntfy/telegram/email alerts were sent."

- Extend `app/activity.py` with a `notification_events` table or separate
  `~/.server-services-manager/notification_log.db`
- Log every fanout: service, recipient, channel, timestamp, success/failure
- New `/alerts` page: filterable timeline of sent notifications
- Per-notifier stats (delivery rate, last failure, latency)
- **Reuses:** activity log DB, notifier module hooks, `/activity` page pattern
- **Tests:** fanout logging, query, stats aggregation

---

## Phase 20 — Package Updates Dashboard (🔜 Next)

> **User says:** "Show pending apt/yum updates, select and install."

- New `app/package_manager.py` — detect distro (apt/dnf/yum), list updates
- `GET /api/packages/updates` — parsed output of `apt list --upgradable`
  or `dnf check-update`
- `POST /api/packages/install` — install selected packages (via background
  process, stream output to SSE)
- New `/packages` page: table of pending updates with checkbox select,
  "Select All", "Install Selected", progress log
- Security updates flagged with a warning badge
- **Refresh model:** manual — `apt update` runs only when the user clicks
  the "Refresh" button. If the cached list is older than 1 hour, the page
  shows a "stale data" warning.
- **Reuses:** SSE streaming, control panel password-reauth pattern
- **Tests:** mock apt/dnf output, install simulation

---

## Phase 21 — Log Viewer with Search/Filter

> **User says:** "Currently shows last 100 lines in a scrollable div.
> Add pagination, grep-style search, date range, severity filter."

- Upgrade the Dashboard's log panel and the system services Logs tab
- Backend: `GET /api/programs/<name>/logs?search=&since=&until=&severity=&offset=&limit=`
- Server-side filtering + pagination (don't send 10K lines to the browser)
- Frontend: text input, date-range picker, severity dropdown, scroll + "load more"
- Same interface for systemd journal logs (`GET /api/system-services/<name>/logs?search=...`)
- **Reuses:** existing log endpoints, journalctl query in `app/system_services.py`
- **Tests:** filter query parsing, pagination logic, combined search + severity

---

## Phase 22 — Firewall Manager

> **User says:** "ufw / firewalld frontend. Enable/disable, add/remove rules, status."

- New `app/firewall_manager.py` — detect ufw or firewalld
- `GET /api/firewall/status` — enabled/disabled, default policy, rules list
- `POST /api/firewall/rules` — add rule (port, protocol, source allow/deny)
- `DELETE /api/firewall/rules/<id>` — remove rule
- `POST /api/firewall/enable` / `POST /api/firewall/disable`
- New `/firewall` page: status card, rules table with toggle enable/disable,
  add-rule form (port number, protocol dropdown, source IP, allow/deny)
- **Reuses:** sudo password auth pattern (same as system services),
  control panel confirmation modals
- **Tests:** mock iptables/ufw, rule parsing, add/remove round-trip

---

## Phase 23 — Backup Scheduler

> **User says:** "Set up periodic backups of config, databases, or
> directories via systemd timers."

- **Destinations in v1:** local disk only. Remote destinations (S3, SFTP)
  deferred — easy to add later as a destination plugin.
- Extend `app/schedules.py` (the systemd timer infrastructure) with a
  backup-specific UI
- New `/backups` page:
  - List existing backup jobs (reading from systemd timers)
  - Create new: source path or database URL, destination path, schedule
    expression, retention count
  - One-shot "run now" button
  - Status: last run, last size, next run, success/failure
- Backups are just `ssm-backup-<name>.service` + `ssm-backup-<name>.timer`
  units (oneshot `tar` / `pg_dump` etc.)
- **Reuses:** `app/schedules.py`, systemd timer lifecycle, activity log
- **Tests:** timer creation, backup unit file validation

---

## Phase 24 — API Documentation

> **User says:** "Swagger/OpenAPI for the 40+ REST endpoints."

- Add `Flask-Swagger-UI` or `flasgger` or generate OpenAPI spec manually
- Annotate routes with `@swag_from` or inline YAML
- Accessible at `/docs` (serves Swagger UI)
- Covers: programs CRUD, system services, file manager, packages, docker,
  firewall, backups
- **Value:** Makes the API usable for scripting / automation without
  reading source code
- **Tests:** spec is valid JSON Schema, every documented endpoint responds

---

## Phase 25 — Disk Usage Analyzer

> **User says:** "Visual breakdown per directory (think ncdu in the browser)."

- New `GET /api/disk/usage` — runs `du -sh *` in a given directory
  (chrooted to `$HOME` like the file manager)
- New `/disk` page:
  - Treemap or sunburst chart (use Canvas or a lightweight library)
  - Click a block to drill into that directory
  - Sidebar with largest files, filesystem-level usage bar
- **Reuses:** file manager's chroot logic, monitor's Chart.js
- **Tests:** du output parsing, path validation, chroot enforcement

---

## Phase 26 — SSH Key Manager

> **User says:** "Add/remove authorized keys via UI."

- New `app/ssh_manager.py` — read/write `~/.ssh/authorized_keys`
- `GET /api/ssh/keys` — list keys (comment, fingerprint, algorithm)
- `POST /api/ssh/keys` — add key (paste public key text)
- `DELETE /api/ssh/keys/<id>` — remove key (by comment/fingerprint)
- Upload from file OR paste from clipboard
- Warning before removing last remaining key (lockout protection)
- **Tests:** key parsing (RFC 4253), add/remove round-trip, lockout guard

---

## Phase 27 — Multi-host Cluster Management

> **User says:** "Link multiple devices all running this app on the local
> network, see their state in one dashboard."

- **Scope:** LAN-only. No public-internet support in v1.
- **Architecture:** No central server — every instance is peer-to-peer.
  Each node discovers others via mDNS / Zeroconf (`avahi` / `Bonjour`),
  broadcasting its hostname, port, and a shared cluster secret.
- **Discovery:**
  - mDNS service type: `_ssm-manager._tcp`
  - On startup, browse the network for peers; UI shows a "Nearby nodes" list
  - Manual add by `host:port` as a fallback for networks without mDNS
- **Aggregated dashboard:**
  - New `/cluster` page: cards for each node (hostname, uptime, CPU/memory bar,
    service counts, health status)
  - Click a card → proxy to that node's dashboard (embedded iframe or API proxy)
  - Color indicators: green (reachable), yellow (high load), red (unreachable)
- **Peer API proxy:**
  - `GET /api/cluster/nodes` — list discovered peers
  - `GET /api/cluster/node/<host>/proxy/*` — transparently proxy to the peer's
    own API (auth via shared secret or cookie forwarding)
  - Reachability check: ping each peer every 10s, track last-seen timestamp
- **Security:**
  - Shared plaintext secret in `config.yaml` (`cluster_secret:`)
  - Every peer-to-peer API call includes `X-SSM-Cluster-Secret` header
  - No secrets sent in cleartext on the wire — **HTTPS recommended for
    any production-like deployment**. The app works over plain HTTP
    on trusted home/office LANs.
  - No challenge-response in v1 — a single shared secret is sufficient
    given the LAN-only model
- **Config:**
  ```yaml
  cluster:
    enabled: true
    secret: my-shared-secret       # must match on all peers
    port: 8881                     # same port as the app
    advertise: 192.168.1.100       # optional, override auto-detect
  ```
- **Reuses:** existing REST API, socketio events (aggregate per-peer),
  dashboard card rendering pattern
- **Tests:** discovery mocks, proxy forwarding, auth handshake, timeout handling

---

## Phase 28 — Full File Explorer

> **User says:** "Better file explorer with file preview etc."

The current `/api/files/*` endpoints support list, upload, download,
delete, and rename/move. Missing:

- **Preview:** tab for text files (syntax-highlighted), image thumbnails,
  video/audio playback, PDF inline view
- **Editor:** Monaco via CDN — full VS Code editing experience with
  syntax highlighting for the common languages (loads from CDN, no build)
- **Tree view:** expandable directory tree in the sidebar (lazy-load)
- **Drag & drop:** upload by dragging files from the OS
- **Bulk operations:** select multiple, delete, move, download as zip
- **Search:** find files by name in the current directory tree
- **Permissions:** show file mode, owner/group, ability to chmod
- **Path breadcrumbs:** clickable directory path above the file list
- **Context menu:** right-click on files for rename, copy, cut, paste,
  download, delete
- **Preview cap:** 50 MB. Files larger than that show "too large to preview"
  and offer download only — avoids browser tab crashes from 2 GB log files

- **Reuses:** existing `/api/files/*` endpoints, chroot logic
- **Frontend:** new `/files` page, self-contained
- **Tests:** preview mime-type detection, path traversal defense, bulk ops

---

## Deferred (not building)

| Feature | Reason |
|---------|--------|
| Multi-user / roles | Adds auth complexity; single-password model is simpler |
| OAuth login | Requires external provider setup; overkill for single-admin |
| Performance / light mode | Low impact — the app is already lightweight |
| Webhook receiver | Outbound webhooks already exist (Phase 12); inbound is niche |
| Profile-based commands | Niche; control panel commands already work |

---

## Suggestions welcome

Open an issue or feature request at:
https://github.com/Rishabh-Bajpai/server-services-manager/issues
