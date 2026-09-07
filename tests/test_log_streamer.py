from unittest.mock import patch, MagicMock

import pytest

from app.log_streamer import LogStreamer, get_streamer


@pytest.fixture(autouse=True)
def _reset_streamer():
    import app.log_streamer as mod
    mod._streamer = None
    with patch.object(LogStreamer, "_start_reader", lambda self, handle: None):
        yield
    if mod._streamer is not None:
        mod._streamer.shutdown()
        mod._streamer = None


def _fake_popen(returncode=0):
    proc = MagicMock()
    proc.poll.return_value = None
    proc.stdout.readline.side_effect = [""]
    proc.wait.return_value = returncode
    return proc


class TestValidation:
    def test_invalid_priority_raises(self):
        s = LogStreamer()
        with pytest.raises(ValueError, match="invalid priority"):
            s.subscribe("foo.service", "sid1", priority="bogus")

    def test_invalid_unit_newline(self):
        s = LogStreamer()
        with pytest.raises(ValueError, match="invalid unit"):
            s.subscribe("foo\nbar.service", "sid1")

    def test_invalid_unit_space(self):
        s = LogStreamer()
        with pytest.raises(ValueError, match="invalid unit"):
            s.subscribe("foo bar.service", "sid1")

    def test_empty_unit(self):
        s = LogStreamer()
        with pytest.raises(ValueError, match="invalid unit"):
            s.subscribe("", "sid1")


class TestSubscribe:
    @patch("app.log_streamer.subprocess.Popen")
    def test_first_subscribe_creates_journalctl(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        handle = s.subscribe("cron.service", "sid1", priority="info", lines=100)
        assert handle.unit == "cron.service"
        args, _ = mock_popen.call_args
        cmd = args[0]
        assert "journalctl" in cmd
        assert "-u" in cmd
        assert "cron.service" in cmd
        assert "-f" in cmd
        assert "-p" in cmd
        assert "info" in cmd
        assert "-n" in cmd
        assert "100" in cmd

    @patch("app.log_streamer.subprocess.Popen")
    def test_second_subscribe_shares_stream(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("cron.service", "sid1", priority="info")
        s.subscribe("cron.service", "sid2", priority="info")
        assert mock_popen.call_count == 1

    @patch("app.log_streamer.subprocess.Popen")
    def test_different_priorities_get_separate_streams(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("cron.service", "sid1", priority="err")
        s.subscribe("cron.service", "sid2", priority="info")
        assert mock_popen.call_count == 2

    @patch("app.log_streamer.subprocess.Popen")
    def test_different_units_get_separate_streams(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("cron.service", "sid1")
        s.subscribe("ssh.service", "sid2")
        assert mock_popen.call_count == 2

    @patch("app.log_streamer.os.path.exists", return_value=False)
    @patch("app.log_streamer.subprocess.Popen")
    def test_missing_journalctl(self, mock_popen, mock_exists):
        s = LogStreamer()
        with pytest.raises(RuntimeError, match="journalctl"):
            s.subscribe("foo.service", "sid1")
        assert mock_popen.call_count == 0
        assert s.active_streams() == []

    @patch("app.log_streamer.subprocess.Popen", side_effect=OSError("no journalctl"))
    def test_popen_failure_handled(self, mock_popen):
        s = LogStreamer()
        with pytest.raises(RuntimeError):
            s.subscribe("foo.service", "sid1")


class TestUnsubscribe:
    @patch("app.log_streamer.subprocess.Popen")
    def test_unsubscribe_keeps_stream_if_other_subscribers(self, mock_popen):
        proc = _fake_popen()
        mock_popen.return_value = proc
        s = LogStreamer()
        s.subscribe("cron.service", "sid1")
        s.subscribe("cron.service", "sid2")
        s.unsubscribe("cron.service", "sid1")
        assert len(s.active_streams()) == 1
        proc.terminate.assert_not_called()

    @patch("app.log_streamer.subprocess.Popen")
    def test_unsubscribe_last_kills_stream(self, mock_popen):
        proc = _fake_popen()
        mock_popen.return_value = proc
        s = LogStreamer()
        s.subscribe("cron.service", "sid1")
        s.unsubscribe("cron.service", "sid1")
        assert s.active_streams() == []
        proc.terminate.assert_called_once()

    @patch("app.log_streamer.subprocess.Popen")
    def test_unsubscribe_unknown_sid_is_noop(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("cron.service", "sid1")
        s.unsubscribe("cron.service", "sid2")
        assert len(s.active_streams()) == 1

    def test_unsubscribe_unknown_unit_is_noop(self):
        s = LogStreamer()
        s.unsubscribe("never-subscribed.service", "sid1")


class TestDisconnectSid:
    @patch("app.log_streamer.subprocess.Popen")
    def test_disconnect_clears_all_subscriptions(self, mock_popen):
        procs = [_fake_popen(), _fake_popen()]
        mock_popen.side_effect = procs
        s = LogStreamer()
        s.subscribe("a.service", "sid1")
        s.subscribe("b.service", "sid1")
        s.disconnect_sid("sid1")
        assert s.active_streams() == []
        procs[0].terminate.assert_called_once()
        procs[1].terminate.assert_called_once()

    @patch("app.log_streamer.subprocess.Popen")
    def test_disconnect_only_clears_matching_sid(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("a.service", "sid1")
        s.subscribe("a.service", "sid2")
        s.disconnect_sid("sid1")
        assert len(s.active_streams()) == 1


class TestBuffering:
    @patch("app.log_streamer.subprocess.Popen")
    def test_buffer_capped_at_max(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        handle = s.subscribe("cron.service", "sid1")
        for i in range(600):
            handle.fanout_line(f"line {i}")
        assert len(handle.buffer) == 500
        assert handle.buffer[0] == "line 100"
        assert handle.buffer[-1] == "line 599"

    @patch("app.log_streamer.subprocess.Popen")
    def test_late_subscriber_gets_replay(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("cron.service", "sid1")
        handle = s._streams[s._stream_key("cron.service", "info")]
        for i in range(5):
            handle.fanout_line(f"line {i}")
        replay = s.get_replay(handle)
        assert replay == ["line 0", "line 1", "line 2", "line 3", "line 4"]


class TestFanout:
    @patch("app.log_streamer.subprocess.Popen")
    def test_fanout_pushes_to_subscriber_queues(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        handle = s.subscribe("cron.service", "sid1")
        s.subscribe("cron.service", "sid2")
        handle.fanout_line("hello")
        handle.fanout_line("world")
        q1 = s.get_subscriber_queue(handle, "sid1")
        q2 = s.get_subscriber_queue(handle, "sid2")
        assert q1.get_nowait() == "hello"
        assert q1.get_nowait() == "world"
        assert q2.get_nowait() == "hello"
        assert q2.get_nowait() == "world"

    @patch("app.log_streamer.subprocess.Popen")
    def test_slow_subscriber_does_not_block_others(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        handle = s.subscribe("cron.service", "sid1")
        s.subscribe("cron.service", "sid2")
        # Fill sid1's queue
        for i in range(3000):
            handle.fanout_line(f"line {i}")
        # Both subscribers should still be alive
        assert len(handle.subscribers) == 2


class TestActiveStreams:
    @patch("app.log_streamer.subprocess.Popen")
    def test_active_streams_metadata(self, mock_popen):
        mock_popen.return_value = _fake_popen()
        s = LogStreamer()
        s.subscribe("cron.service", "sid1", priority="err")
        s.subscribe("ssh.service", "sid2", priority="info")
        s.subscribe("ssh.service", "sid3", priority="info")
        meta = s.active_streams()
        assert len(meta) == 2
        cron = next(m for m in meta if m["unit"] == "cron.service")
        ssh = next(m for m in meta if m["unit"] == "ssh.service")
        assert cron["subscribers"] == 1
        assert cron["priority"] == "err"
        assert ssh["subscribers"] == 2
        assert ssh["priority"] == "info"


class TestSingleton:
    def test_singleton_returns_same_instance(self):
        a = get_streamer()
        b = get_streamer()
        assert a is b

    def test_reset_creates_new(self):
        a = get_streamer()
        import app.log_streamer as mod
        mod._streamer = None
        b = get_streamer()
        assert a is not b


class TestShutdown:
    @patch("app.log_streamer.subprocess.Popen")
    def test_shutdown_stops_all(self, mock_popen):
        procs = [_fake_popen(), _fake_popen()]
        mock_popen.side_effect = procs
        s = LogStreamer()
        s.subscribe("a.service", "sid1")
        s.subscribe("b.service", "sid2")
        s.shutdown()
        assert s.active_streams() == []
        procs[0].terminate.assert_called()
        procs[1].terminate.assert_called()
