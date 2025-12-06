import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from app.process_manager import ProcessManager, ProgramConfig
from app.terminal_manager import TerminalManager
import logging
import os
import threading
import time
from werkzeug.utils import secure_filename
from flask import send_file, send_from_directory

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FlaskAPI")

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=10*1024*1024)

pm = ProcessManager()
tm = TerminalManager(socketio)

@app.route('/')
def index():
    return render_template('index.html')

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

# File Management Endpoints
@app.route('/api/files', methods=['GET'])
def list_files():
    try:
        path = request.args.get('path', '.')
        # Simple security check to prevent traversing up too far if needed, 
        # but for this tool we assume full access to the project dir is desired.
        # We will root it to the current working directory.
        base_dir = os.path.expanduser('~')
        target_dir = os.path.abspath(os.path.join(base_dir, path))
        
        if not target_dir.startswith(base_dir):
            return jsonify({"error": "Access denied"}), 403
            
        if not os.path.exists(target_dir):
             return jsonify({"error": "Directory not found"}), 404

        items = []
        for entry in os.scandir(target_dir):
            items.append({
                "name": entry.name,
                "is_dir": entry.is_dir(),
                "size": entry.stat().st_size if not entry.is_dir() else 0,
                "modified": entry.stat().st_mtime
            })
        
        # Sort folders first, then files
        items.sort(key=lambda x: (not x['is_dir'], x['name'].lower()))
        
        return jsonify({
            "current_path": os.path.relpath(target_dir, base_dir),
            "files": items
        })
    except Exception as e:
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

def background_thread():
    """Example of how to send server generated events to clients."""
    while True:
        programs_data = []
        for p in pm.get_all_programs():
            programs_data.append({
                "name": p.config.name,
                "status": p.status.value,
                "logs": p.logs[-100:], # Temporarily reduce to 100 to debug payload size
                "restart_count": p.restart_count
            })
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

if __name__ == '__main__':
    # Load config and start programs
    pm.load_config()
    pm.start_all()
    
    # Start background thread for updates
    thread = threading.Thread(target=background_thread, daemon=True)
    thread.start()
    
    try:
        socketio.run(app, host='0.0.0.0', port=8001, allow_unsafe_werkzeug=True)
    except KeyboardInterrupt:
        pm.stop_all()
