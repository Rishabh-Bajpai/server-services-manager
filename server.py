import eventlet
eventlet.monkey_patch()

from flask import Flask, render_template, request, jsonify
from flask_socketio import SocketIO, emit
from app.process_manager import ProcessManager, ProgramConfig
import logging
import os
import threading
import time

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("FlaskAPI")

app = Flask(__name__, template_folder='templates')
app.config['SECRET_KEY'] = 'secret!'
socketio = SocketIO(app, cors_allowed_origins="*", max_http_buffer_size=10*1024*1024)

pm = ProcessManager()

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
