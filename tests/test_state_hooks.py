from unittest.mock import MagicMock

import pytest

from app.process_manager import Program, ProgramConfig, ProgramStatus


@pytest.fixture
def program():
    cfg = ProgramConfig(
        name="api",
        command="/bin/sleep 1",
        cwd="/tmp",
    )
    return Program(cfg)


class TestSetStatus:
    def test_no_change_no_hook(self, program):
        program.on_state_change = MagicMock()
        # Initial status is STOPPED, calling again is a no-op
        program._set_status(ProgramStatus.STOPPED)
        program.on_state_change.assert_not_called()

    def test_change_fires_hook(self, program):
        program.on_state_change = MagicMock()
        program._set_status(ProgramStatus.RUNNING)
        program.on_state_change.assert_called_once()
        args = program.on_state_change.call_args[0]
        assert args[1] == ProgramStatus.STOPPED  # old
        assert args[2] == ProgramStatus.RUNNING  # new

    def test_hook_exception_doesnt_break(self, program):
        def bad_hook(*a, **kw):
            raise RuntimeError("boom")
        program.on_state_change = bad_hook
        # Should not raise
        program._set_status(ProgramStatus.RUNNING)
        assert program.status == ProgramStatus.RUNNING

    def test_no_hook_is_fine(self, program):
        # No hook attached — should not raise
        program._set_status(ProgramStatus.RUNNING)
        assert program.status == ProgramStatus.RUNNING

    def test_initial_state_no_hook_fire(self):
        # Constructing a Program doesn't fire the hook even
        # though the initial state is STOPPED (because _set_status
        # is never called from __init__).
        cfg = ProgramConfig(name="api", command="x", cwd="/tmp")
        p = Program(cfg)
        p.on_state_change = MagicMock()
        # Calling _set_status(STOPPED) when already STOPPED should
        # not fire
        p._set_status(ProgramStatus.STOPPED)
        p.on_state_change.assert_not_called()


class TestStateTransitions:
    def test_stop_to_running(self, program):
        program.on_state_change = MagicMock()
        program._set_status(ProgramStatus.RUNNING)
        assert program.status == ProgramStatus.RUNNING
        # No change → no fire
        program._set_status(ProgramStatus.RUNNING)
        program.on_state_change.assert_called_once()

    def test_running_to_failed(self, program):
        program._set_status(ProgramStatus.RUNNING)
        program.on_state_change = MagicMock()
        program._set_status(ProgramStatus.FAILED)
        program.on_state_change.assert_called_once_with(
            program, ProgramStatus.RUNNING, ProgramStatus.FAILED,
        )

    def test_failed_to_stopped(self, program):
        program._set_status(ProgramStatus.RUNNING)
        program._set_status(ProgramStatus.FAILED)
        program.on_state_change = MagicMock()
        program._set_status(ProgramStatus.STOPPED)
        program.on_state_change.assert_called_once_with(
            program, ProgramStatus.FAILED, ProgramStatus.STOPPED,
        )

    def test_full_cycle(self, program):
        events = []
        program.on_state_change = lambda p, old, new: events.append((old.value, new.value))
        program._set_status(ProgramStatus.RUNNING)
        program._set_status(ProgramStatus.FAILED)
        program._set_status(ProgramStatus.STOPPED)
        program._set_status(ProgramStatus.RUNNING)
        assert events == [
            ("stopped", "running"),
            ("running", "failed"),
            ("failed", "stopped"),
            ("stopped", "running"),
        ]


class TestThreadSafety:
    def test_concurrent_set_status(self, program):
        import threading
        # Set up hook that just appends to a list
        events = []
        program.on_state_change = lambda p, old, new: events.append(new.value)
        threads = []
        # Many threads racing to set the status
        for i in range(20):
            t = threading.Thread(target=lambda: program._set_status(ProgramStatus.RUNNING))
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        # All transitions were to RUNNING, so we expect at most
        # one event (the first one that succeeded; subsequent calls
        # see the same status and don't fire).
        assert len(events) <= 1


class TestHookIntegration:
    def test_attach_to_multiple_programs(self):
        cfgs = [
            ProgramConfig(name=f"svc{i}", command="x", cwd="/tmp")
            for i in range(5)
        ]
        progs = [Program(c) for c in cfgs]
        events = []
        def hook(p, old, new):
            events.append((p.config.name, old.value, new.value))
        for p in progs:
            p.on_state_change = hook
        # Start them all
        for p in progs:
            p._set_status(ProgramStatus.RUNNING)
        assert len(events) == 5
        for i, (name, old, new) in enumerate(events):
            assert name == f"svc{i}"
            assert old == "stopped"
            assert new == "running"
