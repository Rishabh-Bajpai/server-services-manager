import os
import pty
import select
import subprocess
import threading
import time
import logging
from typing import Dict, Optional

logger = logging.getLogger("TerminalManager")

class TerminalSession:
    def __init__(self, session_id: str, socketio, cmd: str = "/bin/bash"):
        self.id = session_id
        self.socketio = socketio
        self.cmd = cmd
        self.fd = None
        self.pid = None
        self.active = False
        self.thread = None

    def start(self):
        # Create a new PTY
        self.pid, self.fd = pty.fork()
        
        if self.pid == 0:
            # Child process
            # Set some environment variables if needed
            env = os.environ.copy()
            env["TERM"] = "xterm-256color"
            os.execvpe(self.cmd, [self.cmd], env)
        else:
            # Parent process
            self.active = True
            self.thread = threading.Thread(target=self._read_loop, daemon=True)
            self.thread.start()
            logger.info(f"Started terminal session {self.id} (PID: {self.pid})")

    def _read_loop(self):
        while self.active:
            try:
                # Wait for data to be available
                r, _, _ = select.select([self.fd], [], [], 0.1)
                if self.fd in r:
                    data = os.read(self.fd, 1024)
                    if not data:
                        break
                    # Emit data to specific room or client
                    # We'll emit to the client who owns this session
                    # For simplicity in this v1, we might just emit to a room named after session_id
                    self.socketio.emit('terminal_output', {'id': self.id, 'data': data.decode('utf-8', errors='ignore')})
            except OSError:
                break
            except Exception as e:
                logger.error(f"Error reading from terminal {self.id}: {e}")
                break
        
        self.close()

    def write(self, data: str):
        if self.active and self.fd:
            try:
                os.write(self.fd, data.encode('utf-8'))
            except OSError:
                self.close()

    def resize(self, cols: int, rows: int):
        if self.active and self.fd:
            try:
                import fcntl
                import termios
                import struct
                winsize = struct.pack("HHHH", rows, cols, 0, 0)
                fcntl.ioctl(self.fd, termios.TIOCSWINSZ, winsize)
            except Exception as e:
                logger.error(f"Failed to resize terminal {self.id}: {e}")

    def close(self):
        if not self.active:
            return
            
        self.active = False
        logger.info(f"Closing terminal session {self.id}")
        
        if self.fd:
            try:
                os.close(self.fd)
            except OSError:
                pass
            self.fd = None

        if self.pid:
            try:
                os.kill(self.pid, 9) # Force kill if needed, or wait
                os.waitpid(self.pid, os.WNOHANG)
            except OSError:
                pass
            self.pid = None
            
        self.socketio.emit('terminal_closed', {'id': self.id})

class TerminalManager:
    def __init__(self, socketio):
        self.socketio = socketio
        self.sessions: Dict[str, TerminalSession] = {}

    def create_session(self, session_id: str):
        if session_id in self.sessions:
            return # Already exists
            
        session = TerminalSession(session_id, self.socketio)
        session.start()
        self.sessions[session_id] = session

    def write(self, session_id: str, data: str):
        if session_id in self.sessions:
            self.sessions[session_id].write(data)

    def resize(self, session_id: str, cols: int, rows: int):
        if session_id in self.sessions:
            self.sessions[session_id].resize(cols, rows)

    def close(self, session_id: str):
        if session_id in self.sessions:
            self.sessions[session_id].close()
            del self.sessions[session_id]
