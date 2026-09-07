# Hardening checklist (borrowed structure from Laranode installer)

Laranode's `laranode-installer.sh` is a good reference for fresh-VPS
defaults: scoped sudoers, tight file permissions, UFW allow-list,
and systemd units. This checklist ports those ideas to
server-services-manager without copying any site-specific values.
No secrets, hostnames, or user names are stored here.

## 1. Scoped sudo (do not grant ALL)

Prefer per-command `NOPASSWD` over full sudo. Example pattern only —
replace `<user>` and `<repo>` with your own paths:

```text
<user> ALL=(ALL) NOPASSWD: /usr/bin/systemctl start ssm-*.service, /usr/bin/systemctl stop ssm-*.service
```

The app itself already pipes the login password to `sudo -S` for one
command at a time (see `app/system_services.py`, `app/cron_manager.py`,
`app/firewall_manager.py`). Keep it that way; never run Flask as root.

## 2. File permissions

```bash
# State dir (activity.db, monitor.json, cluster.json, backups.json)
chmod 700 ~/.server-services-manager
# Plugins: owner-write only — a writable plugin dir is RCE.
chmod 700 ~/.server-services-manager/plugins
# Service logs: owner-only
chmod 700 ~/.server-services-manager/logs
# systemd user units
chmod 755 ~/.config/systemd/user
```

Reference: Laranode uses `770 dirs / 660 files / 100 scripts`
for panel paths. We use `700` for state because this app is
single-admin and nothing else needs group read.

## 3. Firewall defaults

```bash
sudo ufw allow 22        # SSH — add BEFORE enabling
sudo ufw allow 8881/tcp # server-services-manager (change to your port)
sudo ufw enable
```

Never add a `deny 22` rule without a source restriction; the
`/firewall` page now warns on this via `build_rule_spec()`.

## 4. Service upgrades

- Keep `SECRET_KEY` and `PASSWORD` in `.env` (gitignored), never in docs.
- Run `python tools/check-secrets.py --staged` before every commit.
- Review `journalctl --user -u server-services-manager` after upgrades.
