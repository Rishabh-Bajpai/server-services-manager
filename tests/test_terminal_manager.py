import os
import struct
import threading
import signal
from unittest.mock import MagicMock, patch

import pytest

from app.terminal_manager import TerminalManager, TerminalSession


@pytest.fixture
def mock_socketio():
    return MagicMock()


class TestTerminalSession:
    def test_start_parent_branch(self, mock_socketio):
        """Parent PID (non-zero fork) starts thread and marks active."""
        with patch("app.terminal_manager.pty.fork", return_value=(1234, 42)), \
             patch("app.terminal_manager.threading.Thread") as MockThread:
            sess = TerminalSession("sess1", mock_socketio)
            sess.start()
            assert sess.pid == 1234
            assert sess.fd == 42
            assert sess.active is True
            MockThread.assert_called_once()
            MockThread.return_value.start.assert_called_once()

    def test_start_child_branch_executes(self, mock_socketio):
        """Child PID (fork returns 0) calls execvpe."""
        with patch("app.terminal_manager.pty.fork", return_value=(0, 99)), \
             patch("app.terminal_manager.os.execvpe") as mock_exec, \
             patch("app.terminal_manager.os.chdir"):
            sess = TerminalSession("sess1", mock_socketio)
            sess.start()
            mock_exec.assert_called_once()
            args = mock_exec.call_args[0]
            assert args[0] == "/bin/bash"

    def test_write_active(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = True
        sess.fd = 10
        with patch("app.terminal_manager.os.write") as mock_write:
            sess.write("hello")
            mock_write.assert_called_once_with(10, b"hello")

    def test_write_inactive_noop(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = False
        sess.fd = 10
        with patch("app.terminal_manager.os.write") as mock_write:
            sess.write("hello")
            mock_write.assert_not_called()

    def test_write_closes_on_oserror(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = True
        sess.fd = 10
        sess.pid = 100
        with patch("app.terminal_manager.os.write", side_effect=OSError("eof")), \
             patch.object(sess, "close") as mock_close:
            sess.write("x")
            mock_close.assert_called_once()

    def test_resize_packs_winsize(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = True
        sess.fd = 10
        with patch("app.terminal_manager.fcntl.ioctl") as mock_ioctl:
            sess.resize(80, 24)
            mock_ioctl.assert_called_once()
            args = mock_ioctl.call_args[0]
            assert args[0] == 10
            # struct HHHH rows, cols, 0, 0
            winsize = args[2]
            rows, cols, _, _ = struct.unpack("HHHH", winsize)
            assert rows == 24 and cols == 80

    def test_resize_inactive_noop(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = False
        sess.fd = 10
        with patch("app.terminal_manager.fcntl.ioctl") as mock_ioctl:
            sess.resize(80, 24)
            mock_ioctl.assert_not_called()

    def test_close_idempotent(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = False
        sess.close()
        mock_socketio.emit.assert_not_called()

    def test_close_closes_fd_and_kills(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = True
        sess.fd = 11
        sess.pid = 999
        with patch("app.terminal_manager.os.close") as mock_close, \
             patch("app.terminal_manager.os.kill") as mock_kill, \
             patch("app.terminal_manager.os.waitpid") as mock_wait:
            sess.close()
            mock_close.assert_called_once_with(11)
            mock_kill.assert_called_once_with(999, signal.SIGKILL)
            mock_wait.assert_called_once()
            assert sess.active is False
            assert sess.fd is None
            assert sess.pid is None
            mock_socketio.emit.assert_called_with("terminal_closed", {"id": "s1"})

    def test_read_loop_emits(self, mock_socketio):
        sess = TerminalSession("s1", mock_socketio)
        sess.active = True
        sess.fd = 12
        # select says fd ready, then read returns data, then second loop break via active false
        state = {"called": False}
        def fake_select(*args, **kwargs):
            # after first iteration, set active false to exit
            if not state["called"]:
                state["called"] = True
                return ([12], [], [])
            sess.active = False
            return ([], [], [])
        with patch("app.terminal_manager.select.select", side_effect=fake_select), \
             patch("app.terminal_manager.os.read", return_value=b"hello"), \
             patch.object(sess, "close"):
            # run one iteration manually: call _read_loop but break quickly
            # we let it run in current thread; it will exit after second select
            t = threading.Thread(target=sess._read_loop, daemon=True)
            t.start()
            t.join(timeout=1)
            # should have emitted terminal_output
            mock_socketio.emit.assert_any_call("terminal_output", {"id": "s1", "data": "hello"})


class TestTerminalManager:
    def test_create_session(self, mock_socketio):
        mgr = TerminalManager(mock_socketio)
        with patch("app.terminal_manager.TerminalSession") as MockSess:
            mock_inst = MagicMock()
            MockSess.return_value = mock_inst
            mgr.create_session("a")
            MockSess.assert_called_once_with("a", mock_socketio)
            mock_inst.start.assert_called_once()
            assert "a" in mgr.sessions

    def test_create_duplicate_noop(self, mock_socketio):
        mgr = TerminalManager(mock_socketio)
        with patch("app.terminal_manager.TerminalSession") as MockSess:
            mgr.sessions["a"] = MagicMock()
            mgr.create_session("a")
            MockSess.assert_not_called()

    def test_write_delegates(self, mock_socketio):
        mgr = TerminalManager(mock_socketio)
        mock_sess = MagicMock()
        mgr.sessions["a"] = mock_sess
        mgr.write("a", "hi")
        mock_sess.write.assert_called_once_with("hi")

    def test_resize_delegates(self, mock_socketio):
        mgr = TerminalManager(mock_socketio)
        mock_sess = MagicMock()
        mgr.sessions["a"] = mock_sess
        mgr.resize("a", 80, 24)
        mock_sess.resize.assert_called_once_with(80, 24)

    def test_close_removes(self, mock_socketio):
        mgr = TerminalManager(mock_socketio)
        mock_sess = MagicMock()
        mgr.sessions["a"] = mock_sess
        mgr.close("a")
        mock_sess.close.assert_called_once()
        assert "a" not in mgr.sessions
