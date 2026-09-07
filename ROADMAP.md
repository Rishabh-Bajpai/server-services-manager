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

## Phases 18–28 (✅ Delivered)

| # | Feature | Status |
|---|---------|--------|
| 18 | Docker container management (`app/docker_manager.py`, `/docker`) | ✅ |
| 19 | Alert history / notification log (`app/alert_log.py`, `/alerts`) | ✅ |
| 20 | Package updates dashboard (`app/package_manager.py`, `/packages`) | ✅ |
| 21 | Log viewer with search/filter (`templates/logs.html`) | ✅ |
| 22 | Firewall manager (`app/firewall_manager.py`, `/firewall`) | ✅ |
| 23 | Backup scheduler (`app/backup_manager.py`, `/backups`) | ✅ |
| 24 | API documentation (`app/openapi.py`, `/openapi.json`, `/docs/`) | ✅ |
| 25 | Disk usage analyzer (`app/disk_manager.py`, `/disk`) | ✅ |
| 26 | SSH key manager (`app/ssh_manager.py`, `/ssh`) | ✅ |
| 27 | Multi-host cluster manager (`app/cluster_manager.py`, `/cluster`) | ✅ |
| 28 | Full file explorer (`app/file_explorer.py`, `/files`) | ✅ |

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
