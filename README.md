# Server Services Manager

> A web-based process manager with a real-time system monitor, PTY terminal, file manager, and control panel for Linux servers.

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/python-3.10+-blue.svg" alt="Python Version"/>
  </a>
  <a href="LICENSE.md">
    <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"/>
  </a>
</p>

---

## Features

- **Service Management** — Start, stop, restart, and monitor services with auto-restart on failure
- **System Services** — Browse, start, stop, enable, disable, and edit drop-in overrides for **any systemd unit** on the host (services, timers, sockets, paths, mounts)
- **System Monitor** — Real-time CPU (per-core + averaged chart), memory, swap, disk I/O, network usage, and top processes with sortable columns
- **Control Panel** — Steam Deck–style quick-action buttons for reboot, suspend, lock, disk/memory checks, and custom user-defined commands
- **Web Terminal** — Multi-tab PTY terminal for direct shell access
- **File Manager** — Browse, upload, and download files on the server
- **Real-time Updates** — Live status and logs via WebSockets
- **Process Recovery** — Survives manager restarts; re-attaches to running services automatically
- **Authentication** — Password-protected access with hashed credentials; sudo password reused for privileged actions
- **Responsive UI** — Dark-themed, resizable panes, desktop and mobile-friendly

## Quick Start

```bash
git clone https://github.com/Rishabh-Bajpai/server-services-manager.git
cd server-services-manager
pip install -r requirements.txt
cp .env.example .env          # then set a secure password
./start.sh
```

Open **http://localhost:8881** and log in with your password.

Stop with `./stop.sh`.

## Install as a Service (recommended)

For auto-start on boot and automatic crash recovery:

```bash
bash install-service.sh
journalctl --user -u server-services-manager -f
```

## Screenshots

### Desktop

| Dashboard | System Monitor | Control Panel | System Services |
|-----------|---------------|---------------|-----------------|
| ![Desktop](resources/dashboard-desktop.png) | ![Monitor](resources/monitor-desktop.png) | ![Control](resources/control-desktop.png) | ![System](resources/system-services-desktop.png) |

### Mobile

| Dashboard | System Monitor | Control Panel | System Services |
|-----------|---------------|---------------|-----------------|
| ![Dashboard Mobile](resources/dashboard-mobile.png) | ![Monitor Mobile](resources/monitor-mobile.png) | ![Control Mobile](resources/control-mobile.png) | ![System Mobile](resources/system-services-mobile.png) |

## Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `PASSWORD` | `admin` | Login password |
| `SECRET_KEY` | Auto-generated | Flask session signing key |
| `CORS_ORIGIN` | `*` | Allowed CORS origin |

### Services (`config.yaml`)

Add services via the GUI as shown below:

<p align="center">
  <img src="resources/Config_screenshot.png" alt="Add Service Form" width="500"/>
</p>

Services can also be added manually in `config.yaml`:

```yaml
programs:
  - name: My Service
    command: python my_app.py
    cwd: /home/user/my_app
    autostart: true
    environment:
      MY_VAR: value
```

### Custom Control Panel Commands

You can add your own commands to the control panel via `config.yaml`:

```yaml
commands:
  - id: my-update
    name: "Update System"
    command: "apt update && apt upgrade -y"
    icon: refresh-cw
    auth: true
    # ^ requires password re-entry before executing
```

Available icons: any [Lucide icon](https://lucide.dev/icons) name.

### System Services Privileges

The `/system-services` page manages **all systemd units** on the host, not just services defined in `config.yaml`. Read operations (listing, status, logs, viewing the unit file) work without privileges. Write operations (start, stop, restart, reload, enable, disable, mask, edit) require sudo, which is obtained by piping your app login password to `sudo -S` for that single command.

**This means the app does not need to run as root** — only the user invoking the action needs sudo. The drop-in file editor writes to `/etc/systemd/system/<name>.d/99-manager.conf` (the standard `systemctl edit` location), so vendor-provided unit files are never touched. `daemon-reload` runs automatically after each edit.

## Pages

| Route | Page | Description |
|-------|------|-------------|
| `/` | Dashboard | Manage services, view logs, terminal |
| `/monitor` | System Monitor | Real-time CPU/Memory/Network charts, processes |
| `/control` | Control Panel | Quick system commands |
| `/system-services` | System Services | Browse and control systemd units on the host |

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, Flask, Flask-SocketIO, Eventlet, psutil |
| Frontend | Vanilla JS, Tailwind CSS, Chart.js, xterm.js, Lucide icons |
| Real-time | WebSockets (Socket.IO) |

## Developing

```bash
# Install deps
pip install -r requirements.txt

# Run tests
python -m pytest tests/ -v --tb=short

# Run with auto-reload
FLASK_DEBUG=1 python server.py
```

## License

MIT &copy; 2025 Rishabh Bajpai
