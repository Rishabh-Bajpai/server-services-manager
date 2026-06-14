import signal
import sys

import eventlet
eventlet.monkey_patch()
import queue
import json
import logging
import os
import tempfile
import time
import threading
import yaml

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_socketio import SocketIO
from app.process_manager import ProcessManager, ProgramConfig
from app.terminal_manager import TerminalManager
from app import system_services
from app.log_streamer import get_streamer
from app import cron_manager
from app.health import HealthCheck, HealthMonitor
from app.notifier import build_notifiers, Event as HealthEvent
from app import activity
from app import palette
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import send_file
from dotenv import load_dotenv
import psutil
import subprocess

# Load environment variables
load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FlaskAPI")

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = os.getenv('SECRET_KEY', os.urandom(24).hex())
socketio = SocketIO(app, cors_allowed_origins=os.getenv('CORS_ORIGIN', '*'), max_http_buffer_size=10*1024*1024)

pm = ProcessManager()
tm = TerminalManager(socketio)

# Health monitor reads `health_check` blocks from config.yaml and
# pings each service on a background thread. Notifiers are also
# loaded from `notifications:` in the same file. Both blocks are
# optional — when missing, the monitor runs no checks.
_health_notifiers: list = []
_health_monitor = None  # initialized after pm.load_config()

_password_hash = generate_password_hash(os.getenv('PASSWORD', 'admin'))

@app.before_request
def require_login():
    allowed_routes = ['login', 'static', 'health']
    if request.endpoint not in allowed_routes and 'logged_in' not in session:
        return redirect(url_for('login'))

@app.route('/health')
def health():
    return jsonify({"status": "ok"})

@app.route('/login', methods=['GET', 'POST'])
def login():
    global _password_hash
    if request.method == 'POST':
        password = request.form.get('password')
        
        if check_password_hash(_password_hash, password):
            session['logged_in'] = True
            return redirect(url_for('index'))
        else:
            flash('Invalid password')
            return redirect(url_for('login'))
            
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.pop('logged_in', None)
    return redirect(url_for('login'))

@app.route('/')
def index():
    return render_template('index.html')

@app.route('/monitor')
def monitor():
    return render_template('monitor.html')

@app.route('/control')
def control():
    custom_commands = []
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    if os.path.exists(config_path):
        try:
            with open(config_path) as f:
                cfg = yaml.safe_load(f)
                if cfg and 'commands' in cfg:
                    custom_commands = cfg['commands']
        except Exception:
            pass
    return render_template('control.html', commands=custom_commands)


@app.route('/system-services')
def system_services_page():
    return render_template('system-services.html')

WHITELIST_COMMANDS = {
    "lock":       {"cmd": "loginctl lock-session",          "auth": False, "icon": "lock"},
    "suspend":    {"cmd": "systemctl suspend",               "auth": True,  "icon": "moon"},
    "hibernate":  {"cmd": "systemctl hibernate",            "auth": True,  "icon": "bed"},
    "reboot":     {"cmd": "systemctl reboot",               "auth": True,  "icon": "power"},
    "shutdown":   {"cmd": "systemctl poweroff",             "auth": True,  "icon": "power-off"},
    "disk":       {"cmd": "df -h /",                        "auth": False, "icon": "hard-drive"},
    "memory":     {"cmd": "free -h",                        "auth": False, "icon": "memory-stick"},
    "uptime":     {"cmd": "uptime",                         "auth": False, "icon": "clock"},
    "temp":       {"cmd": "",                                "auth": False, "icon": "thermometer"},
    "net-restart":{"cmd": "systemctl restart NetworkManager","auth": False, "icon": "wifi-off"},
    "net-ip":     {"cmd": "ip -4 addr show | grep inet | awk '{print $NF\" \"$2}'", "auth": False, "icon": "globe"},
}

@app.route('/api/control/run', methods=['POST'])
def control_run():
    data = request.get_json() or {}
    command_id = data.get('command')
    password = data.get('password', '')

    if command_id in WHITELIST_COMMANDS:
        entry = WHITELIST_COMMANDS[command_id]
        cmd = entry['cmd']
    else:
        config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
        found = None
        if os.path.exists(config_path):
            try:
                with open(config_path) as f:
                    cfg = yaml.safe_load(f)
                    if cfg and 'commands' in cfg:
                        for c in cfg['commands']:
                            if c.get('id') == command_id:
                                found = c
                                break
            except Exception:
                pass
        if found:
            entry = found
            cmd = found.get('command', '')
        else:
            return jsonify({"error": "Unknown command"}), 400

    if entry.get('auth', False):
        if not password:
            return jsonify({"error": "auth_required"}), 401
        if not check_password_hash(_password_hash, password):
            return jsonify({"error": "Invalid password"}), 403

    if command_id == 'temp':
        temp = None
        try:
            temps = psutil.sensors_temperatures()
            for key in ('coretemp', 'k10temp', 'cpu-thermal', 'cpu_thermal', 'thinkpad', 'acpitz'):
                if key in temps:
                    temp = round(temps[key][0].current, 1)
                    break
        except Exception:
            pass
        if temp is not None:
            return jsonify({"ok": True, "output": f"CPU Temperature: {temp}°C"})
        return jsonify({"ok": True, "output": "No temperature sensor available"})

    try:
        result = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=30)
        output = result.stdout.strip() or result.stderr.strip() or "Done (no output)"
        return jsonify({"ok": True, "output": output})
    except subprocess.TimeoutExpired:
        return jsonify({"error": "Command timed out"}), 504
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/programs', methods=['GET'])
def get_programs():
    programs = pm.get_all_programs()
    return jsonify([{
        "name": p.config.name,
        "status": p.status.value,
        "restart_count": p.restart_count,
        "last_restart_time": p.last_restart_time
    } for p in programs])

@app.route('/programs/<name>/config', methods=['GET'])
def get_program_config(name):
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    
    return jsonify({
        "name": program.config.name,
        "command": program.config.command,
        "cwd": program.config.cwd,
        "autostart": program.config.autostart,
        "environment": program.config.environment
    })

@app.route('/programs', methods=['POST'])
def add_program():
    data = request.json
    try:
        config = ProgramConfig(
            name=data['name'],
            command=data['command'],
            cwd=data['cwd'],
            autostart=data.get('autostart', False),
            environment=data.get('environment', {})
        )
        pm.add_program(config)
        activity.log("program.add", target=data['name'], status="ok",
                    detail=f"command={data['command']!r} cwd={data['cwd']!r}",
                    ip=request.remote_addr or "")
        return jsonify({"status": "added", "name": data['name']})
    except Exception as e:
        activity.log("program.add", target=(data or {}).get('name', ''), status="error",
                    detail=str(e), ip=request.remote_addr or "")
        return jsonify({"error": str(e)}), 400

@app.route('/programs/<name>', methods=['PUT'])
def edit_program(name):
    data = request.json
    try:
        config = ProgramConfig(
            name=data['name'],
            command=data['command'],
            cwd=data['cwd'],
            autostart=data.get('autostart', False),
            environment=data.get('environment', {})
        )
        pm.edit_program(name, config)
        activity.log("program.edit", target=name, status="ok",
                    detail=f"new_name={data['name']!r}", ip=request.remote_addr or "")
        return jsonify({"status": "updated", "name": data['name']})
    except Exception as e:
        activity.log("program.edit", target=name, status="error",
                    detail=str(e), ip=request.remote_addr or "")
        return jsonify({"error": str(e)}), 400

@app.route('/programs/<name>', methods=['DELETE'])
def delete_program(name):
    try:
        pm.delete_program(name)
        activity.log("program.delete", target=name, status="ok",
                    ip=request.remote_addr or "")
        return jsonify({"status": "deleted", "name": name})
    except ValueError as e:
        activity.log("program.delete", target=name, status="error",
                    detail=str(e), ip=request.remote_addr or "")
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        activity.log("program.delete", target=name, status="error",
                    detail=str(e), ip=request.remote_addr or "")
        return jsonify({"error": str(e)}), 500

@app.route('/programs/<name>/<action>', methods=['POST'])
def control_program(name, action):
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    
    if action == 'start':
        program.start()
    elif action == 'stop':
        program.stop()
    elif action == 'restart':
        program.restart()
    else:
        return jsonify({"error": "Invalid action"}), 400
        
    return jsonify({"status": action, "name": name})

@app.route('/programs/<name>/logs', methods=['GET'])
def get_logs(name):
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    return jsonify({"logs": list(program.logs)})

@app.route('/programs/<name>/logs/download', methods=['GET'])
def download_logs(name):
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    log_text = "\n".join(program.logs)
    return (log_text, 200, {
        "Content-Type": "text/plain; charset=utf-8",
        "Content-Disposition": f'attachment; filename="{name}.log"'
    })

@app.route('/programs/<name>/logs', methods=['DELETE'])
def clear_logs(name):
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    program.logs.clear()
    return jsonify({"status": "cleared"})

# File Management Endpoints
@app.route('/api/files', methods=['GET'])
def list_files():
    try:
        path = request.args.get('path', '.')
        
        # Use simple expansion for base
        base_dir_raw = os.path.expanduser('~')
        base_dir = os.path.realpath(base_dir_raw)
        
        # Resolve target
        target_dir_raw = os.path.join(base_dir, path)
        target_dir = os.path.realpath(target_dir_raw)
        
        logger.info(f"List files request: path='{path}'")
        logger.info(f"Base: raw='{base_dir_raw}', real='{base_dir}'")
        logger.info(f"Target: raw='{target_dir_raw}', real='{target_dir}'")
        
        if not target_dir.startswith(base_dir):
            logger.warning(f"Access denied: {target_dir} is not under {base_dir}")
            return jsonify({
                "error": "Access denied", 
                "details": f"Target {target_dir} is not inside {base_dir}"
            }), 403
            
        if not os.path.exists(target_dir):
             logger.warning(f"Directory not found: {target_dir}")
             return jsonify({
                 "error": "Directory not found",
                 "details": f"Path {target_dir} does not exist"
             }), 404

        items = []
        for entry in os.scandir(target_dir):
            try:
                # Safe access to file stats
                is_dir = entry.is_dir()
                stat = entry.stat()
                size = stat.st_size if not is_dir else 0
                modified = stat.st_mtime
            except OSError as e:
                # Handle broken symlinks or permission errors
                logger.warning(f"Error accessing {entry.name}: {e}")
                is_dir = False
                size = 0
                modified = 0

            items.append({
                "name": entry.name,
                "is_dir": is_dir,
                "size": size,
                "modified": modified
            })
        
        # Sort folders first, then files
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        
        return jsonify({
            "current_path": os.path.relpath(target_dir, base_dir),
            "files": items
        })
    except Exception as e:
        logger.error(f"Error listing files: {str(e)}")
        return jsonify({"error": str(e)}), 500

@app.route('/api/files/upload', methods=['POST'])
def upload_file():
    try:
        if 'file' not in request.files:
            return jsonify({"error": "No file part"}), 400
        
        file = request.files['file']
        path = request.form.get('path', '.')
        
        if file.filename == '':
            return jsonify({"error": "No selected file"}), 400
            
        if file:
            filename = secure_filename(file.filename)
            base_dir = os.path.expanduser('~')
            target_dir = os.path.realpath(os.path.join(base_dir, path))
            
            if not target_dir.startswith(base_dir):
                return jsonify({"error": "Access denied"}), 403

            file.save(os.path.join(target_dir, filename))
            return jsonify({"status": "uploaded", "filename": filename})
            
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/api/files/download', methods=['GET'])
def download_file():
    try:
        path = request.args.get('path', '')
        if not path:
             return jsonify({"error": "No path specified"}), 400
             
        base_dir = os.path.expanduser('~')
        target_path = os.path.realpath(os.path.join(base_dir, path))
        
        if not target_path.startswith(base_dir):
            return jsonify({"error": "Access denied"}), 403
            
        if not os.path.exists(target_path):
            return jsonify({"error": "File not found"}), 404
            
        if os.path.isdir(target_path):
             return jsonify({"error": "Cannot download directory"}), 400

        return send_file(target_path, as_attachment=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

# ---------------------------------------------------------------------------
# System Services (systemd units)
# ---------------------------------------------------------------------------

@app.route('/api/system-services', methods=['GET'])
def api_system_services_list():
    unit_type = request.args.get('type', 'all')
    state = request.args.get('state', 'all')
    search = request.args.get('q', '').strip()
    only_user = request.args.get('user', '').lower() in ('1', 'true', 'yes')

    try:
        units = system_services.list_units(
            unit_type=unit_type, state=state, search=search, only_user=only_user,
        )
        return jsonify({
            "units": [u.to_summary() for u in units],
            "count": len(units),
        })
    except system_services.SystemServicesError as e:
        return jsonify({"error": str(e), "code": e.code}), 500
    except Exception as e:
        logger.error(f"system-services list failed: {e}")
        return jsonify({"error": str(e)}), 500


@app.route('/api/system-services/<path:name>', methods=['GET'])
def api_system_services_detail(name):
    try:
        unit = system_services.get_unit(name)
        if not unit:
            return jsonify({"error": "Unit not found"}), 404
        return jsonify(unit.to_detail())
    except system_services.SystemServicesError as e:
        return jsonify({"error": str(e), "code": e.code}), 500


@app.route('/api/system-services/<path:name>/unit', methods=['GET'])
def api_system_services_unit(name):
    try:
        content = system_services.get_unit_file(name)
        return jsonify({"name": name, "content": content})
    except system_services.SystemServicesError as e:
        http = 404 if e.code == "not_found" else 500
        return jsonify({"error": str(e), "code": e.code}), http


@app.route('/api/system-services/<path:name>/graph', methods=['GET'])
def api_system_services_graph(name):
    try:
        depth = int(request.args.get('depth', '2'))
    except ValueError:
        depth = 2
    return jsonify(system_services.get_dependencies(name, depth))


@app.route('/api/system-services/<path:name>/unit', methods=['PUT'])
def api_system_services_edit(name):
    data = request.get_json(silent=True) or {}
    content = data.get('content', '')
    password = data.get('password', '')

    if not isinstance(content, str):
        return jsonify({"error": "content must be a string"}), 400
    if len(content) > 200_000:
        return jsonify({"error": "content too large"}), 413
    if not password:
        return jsonify({"error": "auth_required"}), 401

    try:
        result = system_services.edit_unit_file(name, content, password)
        # If the content was for [Service]/[Timer] etc, suggest a daemon-reload
        return jsonify(result)
    except system_services.SystemServicesError as e:
        code = e.code
        http = 403 if code == "permission" else 400 if code in ("invalid",) else 500
        return jsonify({"error": str(e), "code": code}), http


@app.route('/api/system-services/<path:name>/<action>', methods=['POST'])
def api_system_services_action(name, action):
    data = request.get_json(silent=True) or {}
    password = data.get('password', '')

    if action not in system_services.ACTIONS:
        return jsonify({"error": f"Unknown action: {action}"}), 400
    if not password:
        return jsonify({"error": "auth_required"}), 401

    try:
        result = system_services.control(name, action, password)
        activity.log(f"systemd.{action}", target=name, status="ok",
                    detail=result.get("output", ""), ip=request.remote_addr or "")
        return jsonify(result)
    except system_services.SystemServicesError as e:
        activity.log(f"systemd.{action}", target=name, status="error",
                    detail=f"{e.code}: {e}", ip=request.remote_addr or "")
        http = 403 if e.code == "permission" else 500
        return jsonify({"error": str(e), "code": e.code}), http


@app.route('/api/system-services/<path:name>/logs', methods=['GET'])
def api_system_services_logs(name):
    try:
        lines = int(request.args.get('lines', '100'))
    except ValueError:
        lines = 100
    try:
        logs = system_services.get_unit_logs(name, lines)
        return jsonify({"name": name, "logs": logs})
    except system_services.SystemServicesError as e:
        return jsonify({"error": str(e), "code": e.code}), 500


@app.route('/api/system-services/<path:name>/logs/stream', methods=['GET'])
def api_system_services_logs_stream(name):
    """Server-Sent Events endpoint for live log lines.

    Each connected client gets its own journalctl -f (the OS subprocess
    is shared via the streamer; only the per-client output queue is
    unique). Lines are written as ``data: <json>\\n\\n`` SSE frames.
    A sentinel ``{"ended": true}`` is sent when the stream closes.
    """
    priority = request.args.get('priority', 'info')
    try:
        lines = int(request.args.get('lines', '100'))
    except ValueError:
        lines = 100
    try:
        from app.log_streamer import get_streamer
        streamer = get_streamer()
        sid = f"sse-{request.remote_addr}-{id(request)}"
        handle = streamer.subscribe(name, sid, priority, lines)
    except (ValueError, RuntimeError) as e:
        return jsonify({"error": str(e)}), 400

    queue_obj = streamer.get_subscriber_queue(handle, sid)
    replay = streamer.get_replay(handle)

    def generate():
        try:
            for line in replay:
                yield f"data: {json.dumps({'line': line, 'unit': name})}\n\n"
            if queue_obj is None:
                return
            while True:
                try:
                    item = queue_obj.get(timeout=15)
                except queue.Empty:
                    # Heartbeat to keep connection alive through proxies
                    yield ": heartbeat\n\n"
                    continue
                if item is None:
                    yield f"data: {json.dumps({'ended': True, 'unit': name})}\n\n"
                    break
                yield f"data: {json.dumps({'line': item, 'unit': name})}\n\n"
        finally:
            streamer.unsubscribe(name, sid, priority)

    return app.response_class(
        generate(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection': 'keep-alive',
        },
    )


# Cron management
@app.route('/cron')
def cron_page():
    return render_template('cron.html')


@app.route('/api/cron', methods=['GET'])
def api_cron_list():
    try:
        jobs = cron_manager.list_all()
        return jsonify({
            "jobs": [j.to_dict() for j in jobs],
            "count": len(jobs),
        })
    except Exception as e:
        return jsonify({"error": str(e)}), 500


@app.route('/api/cron/validate', methods=['POST'])
def api_cron_validate():
    data = request.get_json(silent=True) or {}
    expr = (data.get("expression") or "").strip()
    err = cron_manager.validate_expression(expr)
    return jsonify({
        "valid": err is None,
        "error": err,
        "description": cron_manager.describe_schedule(expr) if err is None else None,
    })


@app.route('/api/cron/toggle', methods=['POST'])
def api_cron_toggle():
    data = request.get_json(silent=True) or {}
    source = (data.get("source") or "").strip()
    line_number = data.get("line_number")
    enabled = bool(data.get("enabled"))
    password = data.get("password", "")
    try:
        result = cron_manager.toggle_system_job(source, int(line_number), enabled, password)
        activity.log("cron.toggle", target=f"{source}:{line_number}",
                    status="ok", detail=f"enabled={enabled}",
                    ip=request.remote_addr or "")
        return jsonify(result)
    except cron_manager.CronError as e:
        activity.log("cron.toggle", target=f"{source}:{line_number}",
                    status="error", detail=f"{e.code}: {e}",
                    ip=request.remote_addr or "")
        http = 403 if e.code == "permission" else 400 if e.code in ("invalid", "auth_required") else 500
        return jsonify({"error": str(e), "code": e.code}), http


@app.route('/api/programs/<name>/autostart', methods=['GET'])
def api_program_autostart_get(name):
    """Return whether the managed service is enabled at boot.

    Runs ``systemctl is-enabled ssm-<name>.service`` to determine
    state, falling back to the in-memory config flag.
    """
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    safe_name = "".join(c for c in name if c.isalnum() or c in "_-.")
    if not safe_name or safe_name != name:
        return jsonify({"error": "Invalid service name"}), 400
    unit_name = f"ssm-{safe_name}.service"
    try:
        proc = subprocess.run(
            ["systemctl", "is-enabled", unit_name],
            capture_output=True, text=True, timeout=5,
        )
        enabled = proc.returncode == 0
    except Exception:
        enabled = False
    return jsonify({
        "name": name,
        "unit": unit_name,
        "autostart": enabled,
    })


@app.route('/api/programs/<name>/autostart', methods=['POST'])
def api_program_autostart(name):
    """Enable or disable systemd-level autostart for a managed service.

    Writes a small systemd unit to /etc/systemd/system/ssm-<name>.service
    that runs the same command, and uses systemctl enable|disable. The
    manager's manual start/stop is unaffected — this just adds a
    matching systemd unit that the bootloader will run on startup.
    """
    program = pm.get_program(name)
    if not program:
        return jsonify({"error": "Program not found"}), 404
    data = request.get_json(silent=True) or {}
    enabled = bool(data.get("enabled"))
    password = data.get("password", "")
    if not password:
        return jsonify({"error": "auth_required"}), 401

    safe_name = "".join(c for c in name if c.isalnum() or c in "_-.")
    if not safe_name or safe_name != name:
        return jsonify({"error": "Invalid service name"}), 400

    unit_name = f"ssm-{safe_name}.service"
    unit_path = f"/etc/systemd/system/{unit_name}"
    try:
        if enabled:
            # Build the [Service] section
            cfg = program.config
            exec_start = cfg.command
            working_dir = cfg.cwd or "/"
            env_lines = "\n".join(
                f'Environment="{k}={v}"' for k, v in (cfg.environment or {}).items()
            )
            unit_content = (
                "[Unit]\n"
                f"Description=Server Services Manager: {name}\n"
                "After=network.target\n"
                "\n"
                "[Service]\n"
                f"WorkingDirectory={working_dir}\n"
                f"ExecStart={exec_start}\n"
                f"{env_lines}\n"
                "Restart=on-failure\n"
                "RestartSec=5\n"
                "\n"
                "[Install]\n"
                "WantedBy=multi-user.target\n"
            )
            # Write via tempfile + sudo cp
            with tempfile.NamedTemporaryFile("w", delete=False, suffix=".service") as tmp:
                tmp.write(unit_content)
                tmp_path = tmp.name
            try:
                os.chmod(tmp_path, 0o644)
                subprocess.run(
                    ["sudo", "-S", "cp", tmp_path, unit_path],
                    input=password + "\n", capture_output=True, text=True, timeout=10,
                )
            finally:
                try: os.unlink(tmp_path)
                except OSError: pass

        # Now enable or disable via systemctl
        action = "enable" if enabled else "disable"
        proc = subprocess.run(
            ["sudo", "-S", "systemctl", action, unit_name],
            input=password + "\n", capture_output=True, text=True, timeout=15,
        )
        if proc.returncode != 0:
            stderr = (proc.stderr or "").strip()
            first = next((ln for ln in stderr.splitlines() if ln.strip() and "password for" not in ln), stderr)
            lower = stderr.lower()
            if "password" in lower or "permission" in lower or "not in" in lower:
                return jsonify({"error": first, "code": "permission"}), 403
            return jsonify({"error": first, "code": "error"}), 500
        if not enabled:
            # Optionally remove the unit file too
            subprocess.run(
                ["sudo", "-S", "rm", "-f", unit_path],
                input=password + "\n", capture_output=True, text=True, timeout=5,
            )
        subprocess.run(
            ["sudo", "-S", "systemctl", "daemon-reload"],
            input=password + "\n", capture_output=True, text=True, timeout=10,
        )
        # Update the in-memory config
        program.config.autostart = enabled
        activity.log(f"autostart.{action}", target=name, status="ok",
                    detail=f"unit={unit_name}", ip=request.remote_addr or "")
        return jsonify({
            "ok": True,
            "output": f"{action}d {unit_name}",
            "autostart": enabled,
        })
    except subprocess.TimeoutExpired:
        activity.log(f"autostart.{action}", target=name, status="error",
                    detail="timeout", ip=request.remote_addr or "")
        return jsonify({"error": "command timed out"}), 504
    except Exception as e:
        activity.log(f"autostart.{action}", target=name, status="error",
                    detail=str(e), ip=request.remote_addr or "")
        return jsonify({"error": str(e)}), 500


@app.route('/notifications')
def notifications_page():
    return render_template('notifications.html')


@app.route('/activity')
def activity_page():
    return render_template('activity.html')


def _control_commands_for_palette():
    """Return the list of custom control commands from config.yaml."""
    config_path = os.path.join(os.path.dirname(__file__), 'config.yaml')
    if not os.path.exists(config_path):
        return []
    try:
        with open(config_path) as f:
            cfg = yaml.safe_load(f)
        return cfg.get('commands', []) if cfg else []
    except Exception:
        return []


def _system_units_for_palette():
    """Return a small list of running systemd units for the palette."""
    try:
        units = system_services.list_units(state="active", unit_type="service")
        return units[:30]
    except Exception:
        return []


@app.route('/api/palette/search', methods=['GET'])
def api_palette_search():
    q = request.args.get('q', '').strip()
    try:
        limit = max(1, min(50, int(request.args.get('limit', '20'))))
    except ValueError:
        limit = 20
    items = palette.build_palette_index(
        get_programs=lambda: pm.get_all_programs(),
        get_control_commands=_control_commands_for_palette,
        get_system_units=_system_units_for_palette,
    )
    results = palette.search(items, q, limit=limit)
    return jsonify({"results": results})


@app.route('/api/programs/export', methods=['GET'])
def api_programs_export():
    """Export the full set of managed services as JSON.

    The shape matches what ``POST /api/programs/import`` accepts, so a
    file produced here can be re-imported on a fresh install to
    reproduce the same fleet.
    """
    payload = {
        "version": 1,
        "exported_at": time.time(),
        "programs": [
            {
                "name": p.config.name,
                "command": p.config.command,
                "cwd": p.config.cwd,
                "autostart": p.config.autostart,
                "environment": p.config.environment,
            }
            for p in pm.get_all_programs()
        ],
    }
    return app.response_class(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={
            "Content-Disposition": 'attachment; filename="services.json"',
        },
    )


@app.route('/api/programs/import', methods=['POST'])
def api_programs_import():
    """Import a JSON export.

    Body: ``{"version": 1, "programs": [...]}``.

    Modes (via ``?mode=replace|merge``, default ``merge``):
    - ``merge``: keep existing programs, add new ones by name
      (overwriting if the name matches).
    - ``replace``: remove all existing programs first, then import.
    """
    data = request.get_json(silent=True) or {}
    if not isinstance(data.get("programs"), list):
        return jsonify({"error": "expected { programs: [...] }"}), 400
    mode = request.args.get("mode", "merge")
    if mode == "replace":
        for p in list(pm.get_all_programs()):
            try:
                pm.delete_program(p.config.name)
            except Exception:
                pass
    added = 0
    skipped = 0
    for entry in data["programs"]:
        try:
            cfg = ProgramConfig(
                name=entry["name"],
                command=entry["command"],
                cwd=entry["cwd"],
                autostart=entry.get("autostart", False),
                environment=entry.get("environment", {}),
            )
            pm.add_program(cfg)
            added += 1
        except Exception as e:
            skipped += 1
            activity.log("program.import.skip", target=entry.get("name", ""),
                        status="error", detail=str(e),
                        ip=request.remote_addr or "")
    activity.log("program.import", target=f"mode={mode}",
                status="ok", detail=f"added={added} skipped={skipped}",
                ip=request.remote_addr or "")
    return jsonify({"ok": True, "added": added, "skipped": skipped, "mode": mode})


@app.route('/api/activity', methods=['GET'])
def api_activity_list():
    action = request.args.get('action') or None
    target = request.args.get('target') or None
    user = request.args.get('user') or None
    status = request.args.get('status') or None
    try:
        since = float(request.args['since']) if 'since' in request.args else None
    except (KeyError, ValueError):
        since = None
    try:
        limit = max(1, min(1000, int(request.args.get('limit', '200'))))
    except ValueError:
        limit = 200
    entries = activity.list_entries(
        action=action, target=target, user=user, status=status,
        since=since, limit=limit,
    )
    return jsonify({
        "entries": entries,
        "summary": activity.count_by_action(),
    })


@app.route('/api/activity/export.csv', methods=['GET'])
def api_activity_export():
    try:
        limit = max(1, min(10000, int(request.args.get('limit', '1000'))))
    except ValueError:
        limit = 1000
    entries = activity.list_entries(limit=limit)
    csv = activity.export_csv(entries)
    return app.response_class(
        csv, mimetype="text/csv",
        headers={"Content-Disposition": f'attachment; filename="activity.csv"'},
    )


@app.route('/api/notifications/state', methods=['GET'])
def api_notifications_state():
    if _health_monitor is None:
        return jsonify({"states": [], "events": []})
    states = [
        {
            "name": s.name,
            "last_state": s.last_state,
            "last_change": s.last_change,
            "last_check": {
                "healthy": s.last_check.healthy,
                "detail": s.last_check.detail,
                "timestamp": s.last_check.timestamp,
            } if s.last_check else None,
        }
        for s in _health_monitor.all_states()
    ]
    events = [
        {
            "service": e.service,
            "kind": e.kind,
            "state": e.state,
            "detail": e.detail,
            "timestamp": e.timestamp,
        }
        for e in _health_monitor.recent_events()
    ]
    return jsonify({"states": states, "events": events})


_process_cache = {}

def background_thread():
    global _process_cache
    num_cores = psutil.cpu_count() or 1
    while True:
        programs_data = []
        for p in pm.get_all_programs():
            cpu = None
            memory = None
            pid = p._get_pid()
            if pid:
                try:
                    if pid not in _process_cache:
                        _process_cache[pid] = psutil.Process(pid)
                    proc = _process_cache[pid]
                    cpu = proc.cpu_percent(interval=0)
                    if cpu is None:
                        cpu = 0.0
                    cpu = round(cpu / num_cores, 1)
                    memory = proc.memory_percent()
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    _process_cache.pop(pid, None)
            programs_data.append({
                "name": p.config.name,
                "status": p.status.value,
                "logs": list(p.logs)[-100:],
                "restart_count": p.restart_count,
                "cpu": cpu,
                "memory": memory
            })
        # Clean stale cache entries (PIDs no longer tracked)
        tracked_pids = {p._get_pid() for p in pm.get_all_programs() if p._get_pid()}
        stale = [pid for pid in _process_cache if pid not in tracked_pids]
        for pid in stale:
            _process_cache.pop(pid, None)
        socketio.emit('update', {'data': programs_data})
        socketio.sleep(1)

@socketio.on('connect')
def test_connect():
    logger.info('Client connected')

# Terminal Events
@socketio.on('terminal_create')
def handle_terminal_create(data):
    session_id = data.get('id')
    if session_id:
        tm.create_session(session_id)

@socketio.on('terminal_input')
def handle_terminal_input(data):
    session_id = data.get('id')
    input_data = data.get('data')
    if session_id and input_data:
        tm.write(session_id, input_data)

@socketio.on('terminal_resize')
def handle_terminal_resize(data):
    session_id = data.get('id')
    cols = data.get('cols')
    rows = data.get('rows')
    if session_id and cols and rows:
        tm.resize(session_id, cols, rows)

@socketio.on('terminal_close')
def handle_terminal_close(data):
    session_id = data.get('id')
    if session_id:
        tm.close(session_id)

# Note: log streaming is delivered via Server-Sent Events at
# /api/system-services/<name>/logs/stream. No socketio event handlers
# are required for it.

_net_prev = {}
_disk_io_prev = None

_proc_cpu_cache = {}

def system_monitor_thread():
    global _net_prev, _disk_io_prev, _proc_cpu_cache
    num_cores = psutil.cpu_count() or 1
    psutil.cpu_percent(interval=None)
    psutil.cpu_percent(interval=None, percpu=True)

    while True:
        cpu_total = psutil.cpu_percent(interval=0)
        cpu_cores = psutil.cpu_percent(interval=0, percpu=True)
        mem = psutil.virtual_memory()
        swap = psutil.swap_memory()
        load = os.getloadavg()
        boot = psutil.boot_time()

        disks = []
        for p in psutil.disk_partitions(all=False):
            try:
                u = psutil.disk_usage(p.mountpoint)
                disks.append({
                    "device": p.device,
                    "mount": p.mountpoint,
                    "fstype": p.fstype,
                    "total": u.total,
                    "used": u.used,
                    "free": u.free,
                    "percent": u.percent
                })
            except PermissionError:
                pass

        disk_io = psutil.disk_io_counters()
        if _disk_io_prev is not None:
            disk_read_speed = (disk_io.read_bytes - _disk_io_prev.read_bytes) / 2
            disk_write_speed = (disk_io.write_bytes - _disk_io_prev.write_bytes) / 2
        else:
            disk_read_speed = 0
            disk_write_speed = 0
        _disk_io_prev = disk_io

        net = []
        net_counters = psutil.net_io_counters(pernic=True)
        for iface, counters in net_counters.items():
            if iface == 'lo':
                continue
            prev = _net_prev.get(iface)
            sent_speed = (counters.bytes_sent - prev.bytes_sent) / 2 if prev else 0
            recv_speed = (counters.bytes_recv - prev.bytes_recv) / 2 if prev else 0
            net.append({
                "interface": iface,
                "sent_speed": sent_speed,
                "recv_speed": recv_speed,
                "bytes_sent": counters.bytes_sent,
                "bytes_recv": counters.bytes_recv,
                "packets_sent": counters.packets_sent,
                "packets_recv": counters.packets_recv
            })
            _net_prev[iface] = counters

        procs = []
        seen_pids = set()
        for p in psutil.process_iter(['pid', 'name', 'status']):
            try:
                pid = p.info['pid']
                seen_pids.add(pid)
                name = p.info['name']
                status = p.info['status']
                try:
                    if pid not in _proc_cpu_cache:
                        _proc_cpu_cache[pid] = psutil.Process(pid)
                        _proc_cpu_cache[pid].cpu_percent()
                        cpu = 0.0
                        memory = 0.0
                    else:
                        proc = _proc_cpu_cache[pid]
                        cpu = proc.cpu_percent(interval=0)
                        if cpu is None:
                            cpu = 0.0
                        memory = proc.memory_percent()
                        if memory is None:
                            memory = 0.0
                    procs.append({
                        "pid": pid,
                        "name": name,
                        "cpu": round(cpu / num_cores, 1),
                        "memory": round(memory, 1),
                        "status": status
                    })
                except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                    _proc_cpu_cache.pop(pid, None)
            except (psutil.NoSuchProcess, psutil.AccessDenied, psutil.ZombieProcess):
                pass
        stale = [pid for pid in _proc_cpu_cache if pid not in seen_pids]
        for pid in stale:
            _proc_cpu_cache.pop(pid, None)
        procs.sort(key=lambda x: x['cpu'], reverse=True)
        procs = procs[:15]

        # CPU Temperature (graceful fallback if sensors unavailable)
        cpu_temp = None
        try:
            temps = psutil.sensors_temperatures()
            if 'coretemp' in temps:
                cpu_temp = round(max(t.current for t in temps['coretemp']), 1)
            elif 'k10temp' in temps:
                cpu_temp = round(temps['k10temp'][0].current, 1)
            elif 'cpu-thermal' in temps:
                cpu_temp = round(temps['cpu-thermal'][0].current, 1)
            elif 'cpu_thermal' in temps:
                cpu_temp = round(temps['cpu_thermal'][0].current, 1)
            elif 'thinkpad' in temps:
                cpu_temp = round(temps['thinkpad'][0].current, 1)
            elif 'acpitz' in temps:
                cpu_temp = round(temps['acpitz'][0].current, 1)
        except (AttributeError, FileNotFoundError, KeyError, IndexError, TypeError):
            cpu_temp = None

        sys_info = os.uname()
        socketio.emit('system_stats', {
            "cpu": {
                "percent": cpu_total,
                "cores": cpu_cores,
                "load": [round(l, 2) for l in load],
                "temperature": cpu_temp
            },
            "memory": {
                "total": mem.total,
                "available": mem.available,
                "used": mem.used,
                "percent": mem.percent,
                "swap_total": swap.total,
                "swap_used": swap.used,
                "swap_percent": swap.percent
            },
            "disks": disks,
            "disk_io": {
                "read_speed": disk_read_speed,
                "write_speed": disk_write_speed
            },
            "network": net,
            "processes": procs,
            "system": {
                "hostname": sys_info.nodename,
                "kernel": sys_info.release,
                "uptime": int(time.time() - boot)
            }
        })
        socketio.sleep(2)

_shutdown_called = False

def shutdown_handler(signum=None, frame=None):
    global _shutdown_called
    if _shutdown_called:
        return
    _shutdown_called = True
    logger.info(f"Received signal {signum}. Shutting down...")
    pm.stop_all()
    for sid in list(tm.sessions.keys()):
        tm.close(sid)
    try:
        get_streamer().shutdown()
    except Exception as e:
        logger.warning(f"streamer shutdown: {e}")
    try:
        if globals().get("_health_monitor") is not None:
            globals()["_health_monitor"].stop()
    except Exception as e:
        logger.warning(f"health monitor shutdown: {e}")
    logger.info("Shutdown complete.")
    sys.exit(0)

def _get_health_services():
    """Return [(name, HealthCheck)] pairs for all services with a
    `health_check` block in their config."""
    out = []
    raw = pm.config_data.get("programs", []) if hasattr(pm, "config_data") else []
    for p in pm.get_all_programs():
        cfg = None
        for entry in raw:
            if entry.get("name") == p.config.name:
                cfg = entry.get("health_check")
                break
        if not cfg:
            continue
        check = HealthCheck.from_config(cfg)
        if check is not None:
            out.append((p.config.name, check))
    return out


def _on_health_event(event):
    """Called by the monitor on a healthy<->unhealthy transition.

    Pushes a SocketIO event so the dashboard can show a toast and
    update the per-service status dot.
    """
    try:
        socketio.emit("health_event", {
            "service": event.service,
            "state": event.state,
            "detail": event.detail,
            "timestamp": event.timestamp,
        })
    except Exception as e:
        logger.warning(f"emit health_event failed: {e}")


if __name__ == '__main__':
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    
    # Load config, re-attach to surviving processes, then start new ones
    pm.load_config()
    pm.load_state_and_reattach()
    pm.start_all()

    # Initialize and start the health monitor (background health checks
    # and notification fanout)
    cfg_data = pm.config_data if hasattr(pm, "config_data") else {}
    _health_notifiers[:] = build_notifiers(cfg_data.get("notifications", []))
    globals()["_health_monitor"] = HealthMonitor(
        get_services=_get_health_services,
        notifiers=_health_notifiers,
        on_event=_on_health_event,
    )
    globals()["_health_monitor"].start()

    # Start background thread for updates
    thread = threading.Thread(target=background_thread, daemon=True)
    thread.start()

    # Start system monitor thread
    monitor_thread = threading.Thread(target=system_monitor_thread, daemon=True)
    monitor_thread.start()
    
    logger.info(f"Server starting on 0.0.0.0:8881")
    try:
        socketio.run(app, host='0.0.0.0', port=8881, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        shutdown_handler()
