"""Tests for the /config page and /api/config endpoint."""
import json
import os
import tempfile
from unittest.mock import patch

import pytest


@pytest.fixture
def config_client(tmp_path, monkeypatch):
    """Build a Flask test client with a temp config.yaml that we
    can rewrite per-test via the ``write`` callable.
    """
    import sys
    server_mod = sys.modules.get('server')
    if server_mod is None:
        from server import app
        server_mod = sys.modules['server']
    config_path = tmp_path / "config.yaml"
    config_path.write_text("programs: []\n")
    # Point the route at the temp file
    monkeypatch.setattr(server_mod, "__file__", str(tmp_path / "server.py"))
    app = server_mod.app
    app.config['TESTING'] = True
    with app.test_client() as c:
        def write(text):
            config_path.write_text(text)
        yield c, write, config_path


def _login(client):
    with client.session_transaction() as sess:
        sess['logged_in'] = True


class TestConfigEndpoint:
    def test_valid_empty_config(self, config_client):
        cli, _, _ = config_client
        _login(cli)
        r = cli.get("/api/config")
        assert r.status_code == 200
        data = r.get_json()
        assert data["exists"] is True
        assert data["valid"] is True
        assert data["errors"] == []
        assert data["warnings"] == []
        assert data["raw"] == {"programs": []}

    def test_invalid_health_check_type(self, config_client):
        cli, write, _ = config_client
        write("""
programs:
  - name: bad
    command: "echo"
    cwd: /tmp
    health_check:
      type: "made-up-type"
      target: "http://localhost"
""")
        _login(cli)
        r = cli.get("/api/config")
        assert r.status_code == 200
        data = r.get_json()
        assert data["valid"] is False
        assert len(data["errors"]) == 1
        err = data["errors"][0]
        assert "health_check.type" in err["loc"]
        assert "http" in err["msg"]

    def test_yaml_parse_error(self, config_client):
        cli, write, _ = config_client
        write("programs: [unclosed")
        _login(cli)
        r = cli.get("/api/config")
        assert r.status_code == 200
        data = r.get_json()
        assert data["valid"] is False
        assert any("YAML" in e["msg"] for e in data["errors"])

    def test_missing_file(self, config_client):
        cli, write, config_path = config_client
        config_path.unlink()
        _login(cli)
        r = cli.get("/api/config")
        assert r.status_code == 200
        data = r.get_json()
        assert data["exists"] is False
        assert data["valid"] is True

    def test_secrets_redacted(self, config_client):
        cli, write, _ = config_client
        write("""
notifications:
  - type: webhook
    url: https://example.com/hook
    password: "supersecret"
    token: "abc123"
""")
        _login(cli)
        r = cli.get("/api/config")
        data = r.get_json()
        assert data["valid"] is True
        n = data["raw"]["notifications"][0]
        assert n["password"] == "***"
        assert n["token"] == "***"
        # Non-secret fields are NOT redacted
        assert n["url"] == "https://example.com/hook"
        assert n["type"] == "webhook"

    def test_requires_login(self, config_client):
        cli, _, _ = config_client
        # No _login() call — should redirect to /login
        r = cli.get("/api/config")
        assert r.status_code in (302, 401)
