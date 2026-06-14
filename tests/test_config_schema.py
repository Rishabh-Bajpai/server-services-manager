import pytest
from pydantic import ValidationError

from app.config_schema import (
    ProgramConfigSchema, HealthCheckSchema, CommandSchema,
    NtfyConfigSchema, WebhookConfigSchema, TelegramConfigSchema,
    EmailConfigSchema, validate_config,
)


class TestProgramSchema:
    def test_minimal_valid(self):
        p = ProgramConfigSchema(name="api", command="python x", cwd="/tmp")
        assert p.name == "api"
        assert p.autostart is False
        assert p.environment == {}

    def test_full_valid(self):
        p = ProgramConfigSchema(
            name="api-server", command="python -m api", cwd="/srv/api",
            autostart=True, environment={"DEBUG": "0", "PORT": "8000"},
        )
        assert p.autostart is True
        assert p.environment["PORT"] == "8000"

    def test_invalid_name_chars(self):
        with pytest.raises(ValidationError) as exc:
            ProgramConfigSchema(name="bad name!", command="x", cwd="/tmp")
        assert "letters" in str(exc.value).lower()

    def test_empty_command_rejected(self):
        with pytest.raises(ValidationError):
            ProgramConfigSchema(name="x", command="", cwd="/tmp")

    def test_empty_cwd_rejected(self):
        with pytest.raises(ValidationError):
            ProgramConfigSchema(name="x", command="x", cwd="")

    def test_extra_fields_allowed(self):
        p = ProgramConfigSchema(
            name="x", command="x", cwd="/tmp",
            future_field="value", another=42,
        )
        assert p.name == "x"  # doesn't break


class TestHealthCheckSchema:
    def test_http_minimal(self):
        h = HealthCheckSchema(type="http", target="http://x/health")
        assert h.interval == 30
        assert h.timeout == 5

    def test_cmd(self):
        h = HealthCheckSchema(type="cmd", target="true", interval=60)
        assert h.interval == 60

    def test_invalid_type(self):
        with pytest.raises(ValidationError):
            HealthCheckSchema(type="bogus", target="x")

    def test_interval_too_small(self):
        with pytest.raises(ValidationError):
            HealthCheckSchema(type="http", target="x", interval=1)


class TestCommandSchema:
    def test_valid(self):
        c = CommandSchema(id="deploy", name="Deploy", command="./deploy.sh")
        assert c.id == "deploy"
        assert c.icon == "terminal"
        assert c.auth is False

    def test_invalid_id_chars(self):
        with pytest.raises(ValidationError):
            CommandSchema(id="bad id!", name="X", command="x")

    def test_explicit_auth(self):
        c = CommandSchema(id="restart", name="Restart", command="reboot", auth=True)
        assert c.auth is True


class TestNotifierSchemas:
    def test_ntfy(self):
        n = NtfyConfigSchema(type="ntfy", topic="alerts")
        assert n.server == "https://ntfy.sh"

    def test_webhook(self):
        w = WebhookConfigSchema(type="webhook", url="https://x/y")
        assert w.headers is None

    def test_telegram(self):
        t = TelegramConfigSchema(type="telegram", bot_token="1:2", chat_id="3")
        assert t.bot_token == "1:2"

    def test_email_valid(self):
        e = EmailConfigSchema(
            type="email", host="smtp.x", port=587, to=["a@b", "c@d"],
        )
        assert e.use_tls is True

    def test_email_default_port(self):
        e = EmailConfigSchema(type="email", host="smtp.x", to=["a@b"])
        assert e.port == 587


class TestRootConfig:
    def test_empty(self):
        r = validate_config({})
        assert r.programs == []
        assert r.commands == []
        assert r.notifications == []

    def test_full_valid(self):
        cfg = {
            "programs": [
                {"name": "api", "command": "x", "cwd": "/tmp",
                 "health_check": {"type": "http", "target": "http://x"}},
                {"name": "worker", "command": "x", "cwd": "/tmp"},
            ],
            "commands": [
                {"id": "deploy", "name": "Deploy", "command": "x"},
            ],
            "notifications": [
                {"type": "ntfy", "topic": "alerts"},
                {"type": "webhook", "url": "https://x/y"},
            ],
        }
        r = validate_config(cfg)
        assert len(r.programs) == 2
        assert r.programs[0].health_check.type == "http"
        assert len(r.commands) == 1
        assert len(r.notifications) == 2

    def test_duplicate_program_names_rejected(self):
        cfg = {
            "programs": [
                {"name": "dup", "command": "x", "cwd": "/tmp"},
                {"name": "dup", "command": "y", "cwd": "/tmp"},
            ],
        }
        with pytest.raises(ValidationError) as exc:
            validate_config(cfg)
        assert "duplicate" in str(exc.value).lower()

    def test_duplicate_command_ids_rejected(self):
        cfg = {
            "commands": [
                {"id": "dup", "name": "A", "command": "x"},
                {"id": "dup", "name": "B", "command": "y"},
            ],
        }
        with pytest.raises(ValidationError) as exc:
            validate_config(cfg)
        assert "duplicate" in str(exc.value).lower()

    def test_unknown_notifier_type_rejected(self):
        cfg = {
            "notifications": [{"type": "carrier-pigeon"}],
        }
        with pytest.raises(ValidationError):
            validate_config(cfg)

    def test_extra_top_level_allowed(self):
        # Future sections don't break loading
        cfg = {"future_section": {"some": "thing"}, "programs": []}
        r = validate_config(cfg)
        assert r.programs == []
