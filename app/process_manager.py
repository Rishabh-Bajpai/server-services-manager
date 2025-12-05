import subprocess
import threading
import os
import time
import signal
import logging
import yaml
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
        self.status = ProgramStatus.STOPPED
        self.restart_count = 0
        self.last_restart_time = 0
        self.logs: List[str] = []
        self.max_logs = 10000
        self._stop_event = threading.Event()
        self._monitor_thread: Optional[threading.Thread] = None

    def start(self):
        if self.status in [ProgramStatus.RUNNING, ProgramStatus.STARTING]:
            return

        self.status = ProgramStatus.STARTING
        self._stop_event.clear()
        
        env = os.environ.copy()
        env.update(self.config.environment)

        try:
            logger.info(f"Starting program: {self.config.name}")
            # Use preexec_fn=os.setsid to create a new process group for clean killing
            self.process = subprocess.Popen(
                self.config.command,
                cwd=self.config.cwd,
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=env,
                preexec_fn=os.setsid,
                text=True, # Text mode for easier reading
                bufsize=1  # Line buffered
            )
            self.status = ProgramStatus.RUNNING
            self.restart_count = 0 
            self.last_restart_time = time.time()
            
            self._monitor_thread = threading.Thread(target=self._monitor, daemon=True)
            self._monitor_thread.start()
            logger.info(f"Started {self.config.name} with PID {self.process.pid}")

        except Exception as e:
            logger.error(f"Failed to start {self.config.name}: {e}")
            self.status = ProgramStatus.FAILED
            self.log(f"Error starting program: {e}")

    def stop(self):
        if self.status == ProgramStatus.STOPPED:
            return

        self.status = ProgramStatus.STOPPING
        self._stop_event.set()
        
        if self.process:
            logger.info(f"Stopping {self.config.name}...")
            try:
                # Send SIGTERM to process group
                os.killpg(os.getpgid(self.process.pid), signal.SIGTERM)
                try:
                    self.process.wait(timeout=5.0)
                except subprocess.TimeoutExpired:
                    logger.warning(f"Force killing {self.config.name}")
                    os.killpg(os.getpgid(self.process.pid), signal.SIGKILL)
            except ProcessLookupError:
                pass # Already dead
            
            self.process = None
        
        self.status = ProgramStatus.STOPPED
        logger.info(f"Stopped {self.config.name}")

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
        if len(self.logs) > self.max_logs:
            self.logs.pop(0)

class ProcessManager:
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

    def start_all(self):
        for program in self.programs.values():
            if program.config.autostart:
                program.start()

    def stop_all(self):
        logger.info("Stopping all programs...")
        # Threading makes this synchronous effectively if we wait
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
