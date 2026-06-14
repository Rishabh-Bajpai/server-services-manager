import subprocess
import threading
import os
import time
import signal
import json
import logging
import yaml
from collections import deque
from dataclasses import dataclass, field
from typing import List, Optional, Dict
from enum import Enum

# Configure logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logger = logging.getLogger("ProcessManager")

class ProgramStatus(Enum):
    STOPPED = "stopped"
    RUNNING = "running"
    STARTING = "starting"
    STOPPING = "stopping"
    FAILED = "failed"

def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False

@dataclass
class ProgramConfig:
    name: str
    command: str
    cwd: str
    autostart: bool = False
    environment: Dict[str, str] = field(default_factory=dict)

class Program:
    def __init__(self, config: ProgramConfig):
        self.config = config
        self.process: Optional[subprocess.Popen] = None
        self._attached_pid: Optional[int] = None
        self.status = ProgramStatus.STOPPED
        self.restart_count = 0
        self.last_restart_time = 0
        self.logs: deque = deque(maxlen=10000)
        self.max_logs = 10000
        self._stop_event = threading.Event()
        self._lock = threading.Lock()
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self):
        with self._lock:
            if self.status in [ProgramStatus.RUNNING, ProgramStatus.STARTING]:
                return
            if self._monitor_thread and self._monitor_thread.is_alive():
                self._monitor_thread.join(timeout=1)

            self._stop_event.clear()
        
        env = os.environ.copy()
        env.update(self.config.environment)

        try:
            logger.info(f"Starting program: {self.config.name}")
            self.process = subprocess.Popen(
                self.config.command,
                cwd=self.config.cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=os.setsid,
                text=True,
                bufsize=1
            )
            with self._lock:
                self.status = ProgramStatus.RUNNING
                self.last_restart_time = time.time()
            
            self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            self._monitor_thread.start()
            logger.info(f"Started {self.config.name} with PID {self.process.pid}")

        except Exception as e:
            logger.error(f"Failed to start {self.config.name}: {e}")
            with self._lock:
                self.status = ProgramStatus.FAILED
            self.log(f"Error starting program: {e}")

    def _get_pid(self) -> Optional[int]:
        if self.process:
            return self.process.pid
        if self._attached_pid:
            return self._attached_pid
        return None

    def _is_pid_alive(self, pid: int) -> bool:
        return _pid_alive(pid)

    def stop(self):
        with self._lock:
            if self.status == ProgramStatus.STOPPED:
                return
            self._stop_event.set()
        
        pid = self._get_pid()
        if pid:
            logger.info(f"Stopping {self.config.name} (PID: {pid})...")
            try:
                pgid = os.getpgid(pid)
                os.killpg(pgid, signal.SIGTERM)
                for _ in range(5):
                    if not self._is_pid_alive(pid):
                        break
                    time.sleep(1)
                else:
                    logger.warning(f"Force killing {self.config.name}")
                    try:
                        os.killpg(pgid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
            except ProcessLookupError:
                pass
            except OSError:
                try:
                    os.kill(pid, signal.SIGTERM)
                    for _ in range(5):
                        if not _pid_alive(pid):
                            break
                        time.sleep(1)
                    else:
                        os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        
        with self._lock:
            self.process = None
            self._attached_pid = None
            self.status = ProgramStatus.STOPPED
            self.restart_count = 0
        logger.info(f"Stopped {self.config.name}")

    def attach(self, pid: int):
        self._attached_pid = pid
        self._stop_event.clear()
        self.status = ProgramStatus.RUNNING
        self.log(f"Re-attached to running process (PID: {pid})")
        self._monitor_thread = threading.Thread(target=self._monitor_attached, daemon=True)
        self._monitor_thread.start()

    def restart(self):
        self.stop()
        self.start()

    def _monitor(self):
        if not self.process:
            return

        # Helper to read stream
        def read_stream(stream, prefix):
            for line in iter(stream.readline, ''):
                if line:
                    stripped = line.strip()
                    self.log(prefix + stripped)
                    logger.info(f"[{self.config.name}] {prefix}{stripped}") # Debug to console
                else:
                    break
            stream.close()

        # Start threads for stdout and stderr
        stdout_t = threading.Thread(target=read_stream, args=(self.process.stdout, "[OUT] "), daemon=True)
        stderr_t = threading.Thread(target=read_stream, args=(self.process.stderr, "[ERR] "), daemon=True)
        
        stdout_t.start()
        stderr_t.start()

        # Wait for process to exit
        self.process.wait()
        
        # Wait for streams to finish reading
        stdout_t.join()
        stderr_t.join()

        code = self.process.returncode
        logger.info(f"{self.config.name} exited with code {code}")
        
        if self._stop_event.is_set():
            self.status = ProgramStatus.STOPPED
            return

        self.status = ProgramStatus.FAILED
        self.log(f"Process exited with code {code}")
        
        # Auto-restart logic
        self._handle_restart()

    def _monitor_attached(self):
        while self.status == ProgramStatus.RUNNING:
            if self._stop_event.is_set():
                with self._lock:
                    self.status = ProgramStatus.STOPPED
                return
            pid = self._attached_pid
            if pid is None or not self._is_pid_alive(pid):
                try:
                    os.waitpid(pid, os.WNOHANG)
                except OSError:
                    pass
                with self._lock:
                    self.status = ProgramStatus.FAILED
                    self._attached_pid = None
                self.log(f"Process exited (PID: {pid})")
                self._handle_restart()
                return
            time.sleep(1)

    def _handle_restart(self):
        if self._stop_event.is_set():
            return

        current_time = time.time()
        
        if current_time - self.last_restart_time > 60:
             self.restart_count = 0

        self.restart_count += 1
        self.last_restart_time = current_time

        delay = 0
        if self.restart_count > 5:
            delay = 300
            self.log(f"Too many restarts. Waiting {delay} seconds before retrying...")
        else:
            delay = 1 * self.restart_count
            self.log(f"Restarting in {delay} seconds (Attempt {self.restart_count}/5)...")

        # Sleep in chunks to check for stop event
        for _ in range(delay):
            if self._stop_event.is_set():
                return
            time.sleep(1)
            
        if not self._stop_event.is_set():
            self.start()

    def log(self, message: str):
        self.logs.append(message)
        # Best-effort tail persistence: write to a per-service log
        # file. Reading is done on demand via :func:`read_persisted_log`.
        # Failures here are silent — logging is best-effort.
        try:
            from app.log_persistence import append_line
            append_line(self.config.name, message)
        except Exception:
            pass


class ProcessManager:
    STATE_DIR = os.path.expanduser("~/.server-services-manager")
    STATE_FILE = os.path.join(STATE_DIR, "state.json")

    def __init__(self):
        self.programs: Dict[str, Program] = {}
        self.config_path = "config.yaml"
        self.config_data = {}

    def load_config(self, config_path: str = "config.yaml"):
        self.config_path = config_path
        try:
            with open(config_path, "r") as f:
                self.config_data = yaml.safe_load(f) or {}
        except FileNotFoundError:
            self.config_data = {}

        for p_conf in self.config_data.get('programs', []):
            config = ProgramConfig(
                name=p_conf['name'],
                command=p_conf['command'],
                cwd=p_conf['cwd'],
                autostart=p_conf.get('autostart', False),
                environment=p_conf.get('environment', {})
            )
            self.programs[config.name] = Program(config)

    def save_state(self):
        state = []
        for p in self.programs.values():
            pid = p._get_pid()
            if pid and p._is_pid_alive(pid):
                state.append({
                    "name": p.config.name,
                    "pid": pid,
                    "command": p.config.command,
                    "cwd": p.config.cwd,
                    "autostart": p.config.autostart,
                    "environment": p.config.environment
                })
        os.makedirs(self.STATE_DIR, exist_ok=True)
        with open(self.STATE_FILE, "w") as f:
            json.dump({"programs": state}, f)
        logger.info(f"Saved state for {len(state)} running programs")

    def load_state_and_reattach(self):
        try:
            with open(self.STATE_FILE, "r") as f:
                state = json.load(f)
        except (FileNotFoundError, json.JSONDecodeError):
            return

        reattached = 0
        for entry in state.get("programs", []):
            pid = entry.get("pid")
            name = entry.get("name")
            if pid is None or name is None:
                logger.warning(f"Skipping malformed state entry: {entry}")
                continue
            if not _pid_alive(pid):
                logger.info(f"PID {pid} ({name}) no longer alive, skipping re-attach")
                continue
            if name not in self.programs:
                logger.info(f"Program '{name}' from state has no config, skipping")
                continue
            program = self.programs[name]
            program.attach(pid)
            reattached += 1
            logger.info(f"Re-attached to {name} (PID: {pid})")

        if reattached:
            logger.info(f"Re-attached to {reattached} running programs")
        else:
            logger.info("No running programs to re-attach to")

        try:
            os.remove(self.STATE_FILE)
        except OSError:
            pass

    def start_all(self):
        for program in self.programs.values():
            if program.config.autostart:
                program.start()

    def stop_all(self):
        logger.info("Stopping all programs...")
        self.save_state()
        for program in self.programs.values():
            program.stop()

    def add_program(self, config: ProgramConfig):
        if config.name in self.programs:
            raise ValueError(f"Program {config.name} already exists")
        
        self.programs[config.name] = Program(config)
        self._save_program_config(config)

    def edit_program(self, name: str, config: ProgramConfig):
        if name not in self.programs:
            raise ValueError(f"Program {name} not found")
        
        program = self.programs[name]
        
        if program.status != ProgramStatus.STOPPED:
            program.stop()
            
        if name != config.name:
            del self.programs[name]
            self.programs[config.name] = Program(config)
            self.config_data['programs'] = [p for p in self.config_data.get('programs', []) if p['name'] != name]
            self._save_program_config(config)
        else:
            program.config = config
            for i, p in enumerate(self.config_data.get('programs', [])):
                if p['name'] == name:
                    self.config_data['programs'][i] = {
                        'name': config.name,
                        'command': config.command,
                        'cwd': config.cwd,
                        'autostart': config.autostart,
                        'environment': config.environment
                    }
                    break
            self._save_config_file()

    def delete_program(self, name: str):
        if name not in self.programs:
            raise ValueError(f"Program {name} not found")
        
        program = self.programs[name]
        if program.status != ProgramStatus.STOPPED:
            program.stop()
            
        del self.programs[name]
        self.config_data['programs'] = [p for p in self.config_data.get('programs', []) if p['name'] != name]
        self._save_config_file()

    def _save_program_config(self, config: ProgramConfig):
        new_entry = {
            'name': config.name,
            'command': config.command,
            'cwd': config.cwd,
            'autostart': config.autostart,
            'environment': config.environment
        }
        
        if 'programs' not in self.config_data:
            self.config_data['programs'] = []
            
        self.config_data['programs'].append(new_entry)
        self._save_config_file()

    def _save_config_file(self):
        with open(self.config_path, 'w') as f:
            yaml.dump(self.config_data, f)

    def get_program(self, name: str) -> Optional[Program]:
        return self.programs.get(name)

    def get_all_programs(self) -> List[Program]:
        return list(self.programs.values())
