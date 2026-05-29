import os
import json
import tempfile
import yaml
import pytest
from unittest.mock import patch, MagicMock, PropertyMock

from app.process_manager import ProcessManager, ProgramConfig, Program, ProgramStatus


class TestProgramConfig:
    def test_create_config(self, program_config):
        assert program_config.name == "my-program"
        assert program_config.command == "sleep 10"
        assert program_config.cwd == "/tmp"
        assert program_config.autostart is False
        assert program_config.environment == {}


class TestProgram:
    def test_initial_state(self, program_config):
        p = Program(program_config)
        assert p.status == ProgramStatus.STOPPED
        assert p.process is None
        assert p._attached_pid is None
        assert len(p.logs) == 0
        assert p.restart_count == 0

    def test_log_adds_messages(self, program_config):
        p = Program(program_config)
        p.log("line 1")
        p.log("line 2")
        assert len(p.logs) == 2
        assert p.logs[0] == "line 1"
        assert p.logs[1] == "line 2"

    def test_log_respects_maxlen(self, program_config):
        p = Program(program_config)
        from collections import deque
        p.logs = deque(maxlen=3)
        for i in range(5):
            p.log(f"line {i}")
        assert len(p.logs) == 3
        assert p.logs[0] == "line 2"
        assert p.logs[-1] == "line 4"

    def test_is_pid_alive(self, program_config):
        p = Program(program_config)
        assert p._is_pid_alive(os.getpid()) is True
        assert p._is_pid_alive(999999999) is False

    def test_get_pid_returns_none_when_not_running(self, program_config):
        p = Program(program_config)
        assert p._get_pid() is None

    def test_attach_sets_state(self, program_config):
        p = Program(program_config)
        with patch.object(p, '_is_pid_alive', return_value=True):
            p.attach(os.getpid())
            assert p._attached_pid == os.getpid()
            assert p.status == ProgramStatus.RUNNING

    def test_double_start_ignored(self, program_config):
        p = Program(program_config)
        p.status = ProgramStatus.RUNNING
        with patch.object(p, '_stop_event'):
            p.start()  # should return early, no crash
            assert p.status == ProgramStatus.RUNNING


class TestProcessManager:
    def test_load_config_creates_programs(self, temp_config):
        pm = ProcessManager()
        pm.load_config(temp_config)
        assert "test-service" in pm.programs
        assert pm.programs["test-service"].config.command == "echo hello"

    def test_load_config_empty_file(self):
        with tempfile.NamedTemporaryFile(mode='w', suffix='.yaml', delete=False) as f:
            f.write("")
            path = f.name
        pm = ProcessManager()
        pm.load_config(path)
        assert len(pm.programs) == 0
        os.unlink(path)

    def test_load_config_missing_file(self):
        pm = ProcessManager()
        pm.load_config("/nonexistent/config.yaml")
        assert len(pm.programs) == 0

    def test_add_program(self, process_manager, program_config):
        process_manager.add_program(program_config)
        assert "my-program" in process_manager.programs

    def test_add_duplicate_raises(self, process_manager, program_config):
        process_manager.add_program(program_config)
        with pytest.raises(ValueError, match="already exists"):
            process_manager.add_program(program_config)

    def test_get_program(self, process_manager):
        p = process_manager.get_program("test-service")
        assert p is not None
        assert p.config.name == "test-service"

    def test_get_program_not_found(self, process_manager):
        assert process_manager.get_program("nonexistent") is None

    def test_get_all_programs(self, process_manager):
        programs = process_manager.get_all_programs()
        assert len(programs) == 1
        assert programs[0].config.name == "test-service"

    def test_delete_program(self, process_manager):
        process_manager.delete_program("test-service")
        assert "test-service" not in process_manager.programs

    def test_delete_nonexistent_raises(self, process_manager):
        with pytest.raises(ValueError, match="not found"):
            process_manager.delete_program("nonexistent")

    def test_edit_program_rename(self, process_manager):
        new_config = ProgramConfig(
            name="renamed-service",
            command="echo hello",
            cwd="/tmp",
            autostart=False,
            environment={}
        )
        process_manager.edit_program("test-service", new_config)
        assert "test-service" not in process_manager.programs
        assert "renamed-service" in process_manager.programs

    def test_edit_program_update_in_place(self, process_manager):
        new_config = ProgramConfig(
            name="test-service",
            command="echo world",
            cwd="/var",
            autostart=True,
            environment={"BAR": "baz"}
        )
        process_manager.edit_program("test-service", new_config)
        p = process_manager.programs["test-service"]
        assert p.config.command == "echo world"
        assert p.config.cwd == "/var"
        assert p.config.autostart is True
        assert p.config.environment == {"BAR": "baz"}

    def test_edit_nonexistent_raises(self, process_manager, program_config):
        with pytest.raises(ValueError, match="not found"):
            process_manager.edit_program("nonexistent", program_config)

    def test_state_persistence(self, process_manager):
        from app.process_manager import ProcessManager as PM
        state_dir = tempfile.mkdtemp()
        PM.STATE_DIR = state_dir
        PM.STATE_FILE = os.path.join(state_dir, "state.json")

        p = process_manager.programs["test-service"]
        p._attached_pid = 999999  # non-existent PID, won't be saved
        process_manager.save_state()

        with open(PM.STATE_FILE) as f:
            state = json.load(f)
        assert len(state["programs"]) == 0  # no alive PIDs

        import shutil
        shutil.rmtree(state_dir)

    def test_stop_all_saves_state(self, process_manager):
        with patch.object(process_manager, 'save_state') as mock_save:
            process_manager.stop_all()
            mock_save.assert_called_once()
