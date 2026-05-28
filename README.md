# Server Services Manager

> A robust, web-based process manager for controlling server services, with a built-in web terminal and file manager.

<p align="center">
  <img src="resources/readme_screenshot.png" alt="App Screenshot" width="800"/>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/">
    <img src="https://img.shields.io/badge/python-3.8+-blue.svg" alt="Python Version"/>
  </a>
  <a href="https://github.com/Rishabh-Bajpai/server-services-manager/blob/main/LICENSE.md">
    <img src="https://img.shields.io/badge/license-MIT-green.svg" alt="MIT License"/>
  </a>
  <a href="https://github.com/Rishabh-Bajpai/server-services-manager/stargazers">
    <img src="https://img.shields.io/github/stars/Rishabh-Bajpai/server-services-manager" alt="GitHub Stars"/>
  </a>
</p>

---

## Features

- **Service Management** — Start, stop, restart, and monitor long-running services with auto-restart on failure
- **Web Terminal** — Multi-tab PTY terminal for direct server control (bash, vim, htop, any TUI)
- **File Manager** — Browse, upload, and download files on the server
- **Real-time Updates** — Live status and logs via WebSockets
- **Process Recovery** — Survives manager restarts; re-attaches to running services automatically
- **Auto-restart with Backoff** — Smart retry logic: exponential 1-5s, then 300s cooldown
- **Authentication** — Password-protected access with hashed credentials
- **Responsive UI** — Dark-themed, resizable panes, mobile-friendly

## Quick Start

```bash
git clone https://github.com/Rishabh-Bajpai/server-services-manager.git
cd server-services-manager
pip install -r requirements.txt
echo "PASSWORD=your_secure_password" > .env
./start.sh
```

Open **http://localhost:8881** in your browser.

Stop with `./stop.sh`.

## Configuration

### Environment Variables (`.env`)

| Variable | Default | Description |
|----------|---------|-------------|
| `PASSWORD` | `admin` | Login password |
| `SECRET_KEY` | Auto-generated | Flask session signing key |
| `CORS_ORIGIN` | `*` | Allowed CORS origin |

### Services (`config.yaml`)

Services can be added via the web UI or manually in `config.yaml`:

```yaml
programs:
  - name: My Service
    command: python my_app.py
    cwd: /home/user/my_app
    autostart: true
    environment:
      MY_VAR: value
```

## Production Setup (systemd)

For auto-recovery after crashes or reboots:

```bash
# Edit the service file to point to your installation path
sudo cp server-services-manager.service /etc/systemd/system/
sudo systemctl enable server-services-manager
sudo systemctl start server-services-manager
```

## Tech Stack

| Layer | Technology |
|-------|-----------|
| Backend | Python, Flask, Flask-SocketIO, Eventlet |
| Frontend | Vanilla JS, Tailwind CSS, xterm.js |
| Real-time | WebSockets |

## License

MIT &copy; 2025 Rishabh Bajpai
