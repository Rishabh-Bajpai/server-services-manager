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

## Phases 18–24 (✅ Delivered)

| # | Feature | Status |
|---|---------|--------|
| 18 | Docker container management (`app/docker_manager.py`, `/docker`) | ✅ |
| 19 | Alert history / notification log (`app/alert_log.py`, `/alerts`) | ✅ |
| 20 | Package updates dashboard (`app/package_manager.py`, `/packages`) | ✅ |
| 21 | Log viewer with search/filter (`templates/logs.html`) | ✅ |
| 22 | Firewall manager (`app/firewall_manager.py`, `/firewall`) | ✅ |
| 23 | Backup scheduler (`app/backup_manager.py`, `/backups`) | ✅ |
| 24 | API documentation (`app/openapi.py`, `/openapi.json`, `/docs/`) | ✅ |

---

## Phase 25 — Disk Usage Analyzer (🔜 Next)

> **User says:** "Visual breakdown per directory (think ncdu in the browser)."

- New `app/disk_manager.py` — runs `du` with chroot + size caps
- `GET /api/disk/usage?path=&depth=` — returns tree of (name, size, type)
- New `/disk` page:
  - Treemap (Canvas) of children of the current directory
  - Click a block to drill into that directory
  - Sidebar with largest items (top 20 by size)
- **Reuses:** file manager's chroot logic, monitor's Chart.js
- **Tests:** du output parsing, path validation, chroot enforcement

---

## Phase 26 — SSH Key Manager (🔜 Next)

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
