import signal
import sys

import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify, session, redirect, url_for, flash
from flask_socketio import SocketIO, emit
from app.process_manager import ProcessManager, ProgramConfig
from app.terminal_manager import TerminalManager
import logging
import os
import time
import threading
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from flask import send_file, send_from_directory
from dotenv import load_dotenv
import psutil

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
        return jsonify({"status": "added", "name": data['name']})
    except Exception as e:
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
        return jsonify({"status": "updated", "name": data['name']})
    except Exception as e:
        return jsonify({"error": str(e)}), 400

@app.route('/programs/<name>', methods=['DELETE'])
def delete_program(name):
    try:
        pm.delete_program(name)
        return jsonify({"status": "deleted", "name": name})
    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
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
            target_dir = os.path.abspath(os.path.join(base_dir, path))
            
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
        target_path = os.path.abspath(os.path.join(base_dir, path))
        
        if not target_path.startswith(base_dir):
            return jsonify({"error": "Access denied"}), 403
            
        if not os.path.exists(target_path):
            return jsonify({"error": "File not found"}), 404
            
        if os.path.isdir(target_path):
             return jsonify({"error": "Cannot download directory"}), 400

        return send_file(target_path, as_attachment=True)
    except Exception as e:
        return jsonify({"error": str(e)}), 500

_process_cache = {}

def background_thread():
    global _process_cache
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
                    memory = proc.memory_percent()
                except (psutil.NoSuchProcess, psutil.AccessDenied):
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

@socketio.on('disconnect')
def test_disconnect():
    logger.info('Client disconnected')

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

_net_prev = {}
_disk_io_prev = None

def system_monitor_thread():
    global _net_prev, _disk_io_prev
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
            disk_read_speed = disk_io.read_bytes - _disk_io_prev.read_bytes
            disk_write_speed = disk_io.write_bytes - _disk_io_prev.write_bytes
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
            sent_speed = counters.bytes_sent - prev.bytes_sent if prev else 0
            recv_speed = counters.bytes_recv - prev.bytes_recv if prev else 0
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
        for p in psutil.process_iter(['pid', 'name', 'cpu_percent', 'memory_percent', 'status']):
            try:
                info = p.info
                if info['cpu_percent'] is not None:
                    procs.append({
                        "pid": info['pid'],
                        "name": info['name'],
                        "cpu": round(info['cpu_percent'], 1),
                        "memory": round(info['memory_percent'] or 0, 1),
                        "status": info['status']
                    })
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
        procs.sort(key=lambda x: x['cpu'], reverse=True)
        procs = procs[:15]

        sys_info = os.uname()
        socketio.emit('system_stats', {
            "cpu": {
                "percent": cpu_total,
                "cores": cpu_cores,
                "load": [round(l, 2) for l in load]
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

def shutdown_handler(signum=None, frame=None):
    logger.info(f"Received signal {signum}. Shutting down...")
    pm.stop_all()
    for sid in list(tm.sessions.keys()):
        tm.close(sid)
    logger.info("Shutdown complete.")
    sys.exit(0)

if __name__ == '__main__':
    signal.signal(signal.SIGTERM, shutdown_handler)
    signal.signal(signal.SIGINT, shutdown_handler)
    
    # Load config, re-attach to surviving processes, then start new ones
    pm.load_config()
    pm.load_state_and_reattach()
    pm.start_all()
    
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
