import subprocess
import tempfile
import threading as _threading
import time
import os
from unittest.mock import patch, MagicMock
from urllib.error import URLError

import pytest

import app.activity as _activity_mod
from app.notifier import (
    Event, Notifier, NtfyNotifier, WebhookNotifier, TelegramNotifier, EmailNotifier,
    build_notifier, build_notifiers, fanout, _safe_send,
)
from app.health import (
    HealthCheck, CheckResult, ServiceState, HealthMonitor,
    run_check, _run_http, _run_tcp, _run_cmd,
)


@pytest.fixture
def isolated_activity_db(monkeypatch):
    """Redirect the activity DB to a tmpfile so tests don't pollute the live DB.

    Also waits briefly for any pending notifier threads to finish before
    swapping the path, so they write to the same DB the rest of the test sees
    rather than racing the swap and hitting the live DB.
    """
    # Drain in-flight notifier threads from any previous test
    deadline = time.time() + 2.0
    while time.time() < deadline:
        alive = [t for t in _threading.enumerate() if t.name.startswith("notifier-")]
        if not alive:
            break
        time.sleep(0.02)
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    monkeypatch.setattr(_activity_mod, "_DB_PATH", path)
    monkeypatch.setattr(_activity_mod, "_CONN", None)
    yield path
    # Drain again so threads don't outlive the tmpfile
    deadline = time.time() + 2.0
    while time.time() < deadline:
        alive = [t for t in _threading.enumerate() if t.name.startswith("notifier-")]
        if not alive:
            break
        time.sleep(0.02)
    try:
        os.unlink(path)
    except OSError:
        pass


class TestHealthCheck:
    def test_from_config_http(self):
        c = HealthCheck.from_config({
            "type": "http", "target": "http://x/y", "interval": 10, "expect": 200,
        })
        assert c is not None
        assert c.type == "http"
        assert c.interval == 10

    def test_from_config_tcp(self):
        c = HealthCheck.from_config({
            "type": "tcp", "target": "localhost:8080",
        })
        assert c is not None
        assert c.type == "tcp"

    def test_from_config_cmd(self):
        c = HealthCheck.from_config({"type": "cmd", "target": "true"})
        assert c is not None
        assert c.type == "cmd"

    def test_from_config_invalid(self):
        assert HealthCheck.from_config({}) is None
        assert HealthCheck.from_config({"type": "bogus", "target": "x"}) is None
        assert HealthCheck.from_config({"type": "http"}) is None
        assert HealthCheck.from_config({"target": "x"}) is None
        assert HealthCheck.from_config("not a dict") is None

    def test_from_config_clamps_intervals(self):
        c = HealthCheck.from_config({"type": "http", "target": "x", "interval": 1})
        assert c.interval == 5  # min 5
        c = HealthCheck.from_config({"type": "http", "target": "x", "timeout": 0})
        assert c.timeout == 1  # min 1


class TestRunCheck:
    @patch("app.health._run_http")
    def test_dispatches_to_http(self, mock_http):
        mock_http.return_value = CheckResult(True, "ok", 1.0)
        result = run_check(HealthCheck("http", "http://x", 5, 5, 200))
        assert result.healthy
        mock_http.assert_called_once()

    @patch("app.health._run_tcp")
    def test_dispatches_to_tcp(self, mock_tcp):
        mock_tcp.return_value = CheckResult(True, "ok", 1.0)
        run_check(HealthCheck("tcp", "x:80", 5, 5, 0))
        mock_tcp.assert_called_once()

    @patch("app.health._run_cmd")
    def test_dispatches_to_cmd(self, mock_cmd):
        mock_cmd.return_value = CheckResult(True, "ok", 1.0)
        run_check(HealthCheck("cmd", "true", 5, 5, 0))
        mock_cmd.assert_called_once()

    def test_unknown_type(self):
        c = HealthCheck("unknown", "x", 5, 5, 0)
        result = run_check(c)
        assert not result.healthy
        assert "unknown" in result.detail


class TestRunHttp:
    @patch("urllib.request.urlopen")
    def test_success(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.status = 200
        mock_resp.__enter__ = lambda s: s
        mock_resp.__exit__ = MagicMock()
        mock_urlopen.return_value = mock_resp
        c = HealthCheck("http", "http://x", 5, 5, 200)
        result = _run_http(c)
        assert result.healthy
        assert "200" in result.detail

    @patch("urllib.request.urlopen")
    def test_5xx_is_failure(self, mock_urlopen):
        from urllib.error import HTTPError
        mock_urlopen.side_effect = HTTPError("http://x", 500, "err", {}, None)
        c = HealthCheck("http", "http://x", 5, 5, 200)
        result = _run_http(c)
        assert not result.healthy

    @patch("urllib.request.urlopen", side_effect=TimeoutError("slow"))
    def test_timeout(self, mock_urlopen):
        c = HealthCheck("http", "http://x", 5, 5, 200)
        result = _run_http(c)
        assert not result.healthy
        assert "Timeout" in result.detail

    def test_custom_expect_code(self):
        c = HealthCheck("http", "http://x", 5, 5, 404)
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock(status=404)
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock()
            mock_urlopen.return_value = mock_resp
            assert _run_http(c).healthy


class TestRunTcp:
    @patch("socket.create_connection")
    def test_success(self, mock_conn):
        c = HealthCheck("tcp", "host:80", 5, 5, 0)
        result = _run_tcp(c)
        assert result.healthy
        mock_conn.assert_called_once()

    @patch("socket.create_connection", side_effect=ConnectionRefusedError("nope"))
    def test_refused(self, mock_conn):
        c = HealthCheck("tcp", "host:80", 5, 5, 0)
        result = _run_tcp(c)
        assert not result.healthy
        assert "Refused" in result.detail

    def test_bad_target(self):
        c = HealthCheck("tcp", "no_port", 5, 5, 0)
        result = _run_tcp(c)
        assert not result.healthy


class TestRunCmd:
    @patch("subprocess.run")
    def test_success(self, mock_run):
        mock_run.return_value = MagicMock(returncode=0, stdout="all good", stderr="")
        c = HealthCheck("cmd", "true", 5, 5, 0)
        result = _run_cmd(c)
        assert result.healthy
        assert "all good" in result.detail

    @patch("subprocess.run")
    def test_failure(self, mock_run):
        mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="oops")
        c = HealthCheck("cmd", "false", 5, 5, 0)
        result = _run_cmd(c)
        assert not result.healthy
        assert "oops" in result.detail

    @patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="x", timeout=5))
    def test_timeout(self, mock_run):
        c = HealthCheck("cmd", "sleep 10", 5, 5, 0)
        result = _run_cmd(c)
        assert not result.healthy
        assert "timeout" in result.detail


class TestServiceState:
    def test_push_healthy(self):
        s = ServiceState(name="x")
        changed = s.push(CheckResult(True, "ok", 1.0))
        assert changed == "healthy"
        assert s.last_state == "healthy"

    def test_no_change(self):
        s = ServiceState(name="x")
        s.push(CheckResult(True, "ok", 1.0))
        changed = s.push(CheckResult(True, "still ok", 2.0))
        assert changed is None

    def test_transition(self):
        s = ServiceState(name="x")
        s.push(CheckResult(True, "ok", 1.0))
        changed = s.push(CheckResult(False, "down", 2.0))
        assert changed == "unhealthy"

    def test_history_capped(self):
        s = ServiceState(name="x")
        for i in range(60):
            s.push(CheckResult(i % 2 == 0, f"r{i}", float(i)))
        assert len(s.history) == 50


class TestHealthMonitor:
    def test_records_transitions_and_emits_event(self, isolated_activity_db):
        notifier = MagicMock()
        notifier.send = MagicMock(return_value=True)
        captured = []
        mon = HealthMonitor(
            get_services=lambda: [("svc", HealthCheck("http", "http://x", 5, 5, 200))],
            notifiers=[notifier],
            on_event=lambda e: captured.append(e),
        )
        # 1st push: healthy (transition unknown -> healthy)
        mon._record("svc", CheckResult(True, "ok", 1.0))
        # 2nd push: unhealthy (transition)
        mon._record("svc", CheckResult(False, "down", 2.0))
        # 3rd push: back to healthy (transition)
        mon._record("svc", CheckResult(True, "back", 3.0))

        state = mon.state("svc")
        assert state is not None
        assert state.last_state == "healthy"
        # 3 transitions: unknown->healthy, healthy->unhealthy, unhealthy->healthy
        assert len(captured) == 3
        assert notifier.send.call_count == 3

    def test_no_event_when_unchanged(self):
        mon = HealthMonitor(
            get_services=lambda: [],
            notifiers=[],
            on_event=lambda e: None,
        )
        # First push creates the state (transition: unknown -> healthy)
        mon._record("svc", CheckResult(True, "ok", 1.0))
        # Second push: still healthy, no event
        mon._record("svc", CheckResult(True, "ok", 2.0))
        events = mon.recent_events()
        assert len(events) == 1
        assert events[0].state == "healthy"

    def test_recent_events_capped(self):
        mon = HealthMonitor(
            get_services=lambda: [],
            notifiers=[],
            on_event=lambda e: None,
        )
        # Each push creates a new service and a transition event
        for i in range(250):
            mon._record(f"svc{i}", CheckResult(True, "ok", float(i)))
        assert len(mon.recent_events()) == 200


class TestEvent:
    def test_subject(self):
        e = Event(service="api", kind="transition", state="unhealthy", detail="down")
        assert "api" in e.subject()
        assert "unhealthy" in e.subject()

    def test_body_contains_detail(self):
        e = Event(service="api", kind="transition", state="healthy",
                  detail="all good", timestamp=123.0)
        body = e.body()
        assert "api" in body
        assert "all good" in body
        assert "123" in body


class TestNtfyNotifier:
    def test_construct_requires_topic(self):
        with pytest.raises(ValueError):
            NtfyNotifier(topic="")

    def test_send_success(self):
        n = NtfyNotifier(topic="test", server="https://ntfy.sh")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock(status=200)
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock()
            mock_urlopen.return_value = mock_resp
            e = Event(service="x", kind="transition", state="healthy")
            assert n.send(e)
            args = mock_urlopen.call_args[0][0]
            assert "https://ntfy.sh/test" == args.full_url
            assert args.get_full_url().endswith("/test")

    def test_send_failure_doesnt_raise(self):
        n = NtfyNotifier(topic="test")
        with patch("urllib.request.urlopen", side_effect=URLError("net")):
            e = Event(service="x", kind="transition", state="unhealthy")
            assert n.send(e) is False


class TestWebhookNotifier:
    def test_construct_requires_url(self):
        with pytest.raises(ValueError):
            WebhookNotifier(url="")

    def test_send_posts_json(self):
        n = WebhookNotifier(url="https://x/y", headers={"X-Auth": "tok"})
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock(status=200)
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock()
            mock_urlopen.return_value = mock_resp
            e = Event(service="api", kind="transition", state="healthy", detail="ok", timestamp=1.0)
            assert n.send(e)
            req = mock_urlopen.call_args[0][0]
            assert req.get_full_url() == "https://x/y"
            assert req.get_header("X-auth") == "tok"  # header normalization


class TestTelegramNotifier:
    def test_construct_requires_credentials(self):
        with pytest.raises(ValueError):
            TelegramNotifier(bot_token="", chat_id="1")
        with pytest.raises(ValueError):
            TelegramNotifier(bot_token="abc", chat_id="")

    def test_send_posts_to_telegram_api(self):
        n = TelegramNotifier(bot_token="123:abc", chat_id="42")
        with patch("urllib.request.urlopen") as mock_urlopen:
            mock_resp = MagicMock(status=200)
            mock_resp.__enter__ = lambda s: s
            mock_resp.__exit__ = MagicMock()
            mock_urlopen.return_value = mock_resp
            e = Event(service="api", kind="transition", state="healthy")
            assert n.send(e)
            assert mock_urlopen.call_args[0][0].get_full_url().endswith("/sendMessage")


class TestEmailNotifier:
    def test_construct_requires_host(self):
        with pytest.raises(ValueError):
            EmailNotifier(host="", port=587, username="", password="",
                         from_addr="", to_addrs=[], use_tls=False)

    def test_send_uses_smtp(self):
        n = EmailNotifier(host="smtp.x", port=587, username="u", password="p",
                         from_addr="u@x", to_addrs=["r@x"], use_tls=False)
        with patch("smtplib.SMTP") as mock_smtp:
            mock_smtp.return_value.__enter__ = lambda s: s
            mock_smtp.return_value.__exit__ = MagicMock()
            e = Event(service="api", kind="transition", state="healthy")
            assert n.send(e)
            # Verify SMTP was called with our host
            mock_smtp.assert_called_once_with("smtp.x", 587, timeout=10)

    def test_send_smtp_failure(self):
        n = EmailNotifier(host="smtp.x", port=587, username="u", password="p",
                         from_addr="u@x", to_addrs=["r@x"], use_tls=False)
        with patch("smtplib.SMTP", side_effect=OSError("no smtp")):
            e = Event(service="api", kind="transition", state="healthy")
            assert n.send(e) is False


class TestBuildNotifiers:
    def test_builds_ntfy(self):
        n = build_notifier({"type": "ntfy", "topic": "alerts"})
        assert isinstance(n, NtfyNotifier)

    def test_builds_webhook(self):
        n = build_notifier({"type": "webhook", "url": "https://x"})
        assert isinstance(n, WebhookNotifier)

    def test_builds_telegram(self):
        n = build_notifier({"type": "telegram", "bot_token": "1:2", "chat_id": "3"})
        assert isinstance(n, TelegramNotifier)

    def test_builds_email(self):
        n = build_notifier({
            "type": "email", "host": "smtp.x", "to": ["a@b"],
        })
        assert isinstance(n, EmailNotifier)

    def test_builds_email_honors_from_addr(self):
        # Regression: the documented "from_addr" key was silently
        # ignored in favor of "from"/username, so configured senders
        # never took effect.
        n = build_notifier({
            "type": "email", "host": "smtp.x", "to": ["a@b"],
            "from_addr": "ssm@example.com", "username": "u",
        })
        assert isinstance(n, EmailNotifier)
        assert n.from_addr == "ssm@example.com"

    def test_unknown_type(self):
        assert build_notifier({"type": "carrier-pigeon"}) is None

    def test_invalid_config(self):
        assert build_notifier("not a dict") is None
        assert build_notifier({}) is None

    def test_build_list_skips_invalid(self):
        specs = [
            {"type": "ntfy", "topic": "a"},
            {"type": "bogus"},
            {"type": "webhook", "url": "https://x"},
        ]
        out = build_notifiers(specs)
        assert len(out) == 2


class TestFanout:
    def test_fanout_calls_all_notifiers(self):
        n1 = MagicMock(spec=Notifier)
        n2 = MagicMock(spec=Notifier)
        n1.send = MagicMock(return_value=True)
        n2.send = MagicMock(return_value=False)
        e = Event(service="x", kind="transition", state="healthy")
        fanout([n1, n2], e)
        # Wait briefly for threads
        time.sleep(0.1)
        assert n1.send.called
        assert n2.send.called

    def test_safe_send_doesnt_raise(self):
        n = MagicMock(spec=Notifier)
        n.send = MagicMock(side_effect=Exception("boom"))
        e = Event(service="x", kind="transition", state="healthy")
        _safe_send(n, e)  # must not raise
