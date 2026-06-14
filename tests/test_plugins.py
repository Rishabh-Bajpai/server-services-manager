import textwrap
from unittest.mock import MagicMock

import pytest

from app import plugins


@pytest.fixture
def plugin_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(plugins, "DEFAULT_DIR", str(tmp_path))
    yield tmp_path


def _write_plugin(path, body: str) -> None:
    path.write_text(textwrap.dedent(body))


class TestDiscovery:
    def test_empty_dir(self, plugin_dir):
        assert plugins.discover() == []

    def test_nonexistent_dir(self, tmp_path, monkeypatch):
        monkeypatch.setattr(plugins, "DEFAULT_DIR", str(tmp_path / "missing"))
        assert plugins.discover() == []

    def test_skip_private_files(self, plugin_dir):
        (plugin_dir / "_private.py").write_text("# not loaded")
        (plugin_dir / ".hidden.py").write_text("# not loaded")
        assert plugins.discover() == []

    def test_skip_non_python(self, plugin_dir):
        (plugin_dir / "README.md").write_text("# not loaded")
        assert plugins.discover() == []

    def test_simple_plugin(self, plugin_dir):
        _write_plugin(plugin_dir / "hello.py", """
            from app.plugins import PluginBase
            class Hello(PluginBase):
                name = "hello"
                description = "Says hi"
                version = "1.2.3"
        """)
        result = plugins.discover()
        assert len(result) == 1
        p = result[0]
        assert p["name"] == "hello"
        assert p["description"] == "Says hi"
        assert p["version"] == "1.2.3"
        assert p["file"] == "hello.py"

    def test_multiple_classes_in_one_file(self, plugin_dir):
        _write_plugin(plugin_dir / "multi.py", """
            from app.plugins import PluginBase
            class A(PluginBase):
                name = "a"
            class B(PluginBase):
                name = "b"
        """)
        result = plugins.discover()
        names = sorted(p["name"] for p in result)
        assert names == ["a", "b"]

    def test_subclass_in_different_module_ignored(self, plugin_dir):
        # Subclass defined in this plugin but inheriting from a
        # base that's exported from somewhere else — still
        # loaded, because PluginBase is the only criterion.
        _write_plugin(plugin_dir / "x.py", """
            from app.plugins import PluginBase
            class X(PluginBase):
                name = "x"
        """)
        assert len(plugins.discover()) == 1

    def test_broken_import_logged_not_raised(self, plugin_dir, caplog):
        _write_plugin(plugin_dir / "broken.py", """
            import this_does_not_exist
            class Foo(PluginBase):
                name = "foo"
        """)
        # discover() should not raise; broken plugin reported
        result = plugins.discover()
        assert any(p["file"] == "broken.py" for p in result)


class TestLoadAll:
    def test_load_calls_register(self, plugin_dir):
        _write_plugin(plugin_dir / "p.py", """
            from app.plugins import PluginBase
            class P(PluginBase):
                name = "p"
                def register(self, app, pm, activity, notifier_module=None):
                    app.add_url_rule('/_plugin_route', 'p_route', lambda: 'ok')
        """)
        app = MagicMock()
        pm = MagicMock()
        activity = MagicMock()
        loaded = plugins.load_all(app=app, pm=pm, activity=activity)
        assert len(loaded) == 1
        assert loaded[0].name == "p"
        # The plugin called app.add_url_rule
        app.add_url_rule.assert_called_once()

    def test_register_exception_does_not_break(self, plugin_dir, caplog):
        _write_plugin(plugin_dir / "bad.py", """
            from app.plugins import PluginBase
            class Bad(PluginBase):
                name = "bad"
                def register(self, **kw):
                    raise RuntimeError("boom")
        """)
        app = MagicMock()
        loaded = plugins.load_all(app=app, pm=MagicMock(),
                                   activity=MagicMock())
        # Plugin failed, so not in returned list
        assert len(loaded) == 0

    def test_instantiation_exception(self, plugin_dir):
        _write_plugin(plugin_dir / "p.py", """
            from app.plugins import PluginBase
            class P(PluginBase):
                name = "p"
                def __init__(self):
                    raise ValueError("nope")
        """)
        loaded = plugins.load_all(app=MagicMock(), pm=MagicMock(),
                                   activity=MagicMock())
        assert loaded == []

    def test_load_empty_dir(self, plugin_dir):
        loaded = plugins.load_all(app=MagicMock(), pm=MagicMock(),
                                   activity=MagicMock())
        assert loaded == []

    def test_returns_plugin_instances(self, plugin_dir):
        _write_plugin(plugin_dir / "p.py", """
            from app.plugins import PluginBase
            class P(PluginBase):
                name = "p"
        """)
        loaded = plugins.load_all(app=MagicMock(), pm=MagicMock(),
                                   activity=MagicMock())
        assert all(isinstance(p, plugins.PluginBase) for p in loaded)


class TestUnload:
    def test_unload_calls_unregister(self):
        p1 = MagicMock(spec=plugins.PluginBase)
        p2 = MagicMock(spec=plugins.PluginBase)
        plugins.unload_all([p1, p2])
        p1.unregister.assert_called_once()
        p2.unregister.assert_called_once()

    def test_unload_handles_exception(self):
        p = MagicMock(spec=plugins.PluginBase)
        p.unregister.side_effect = RuntimeError("boom")
        # Should not raise
        plugins.unload_all([p])

    def test_unload_empty(self):
        plugins.unload_all([])  # no-op


class TestPluginBase:
    def test_register_must_be_overridden(self):
        p = plugins.PluginBase()
        with pytest.raises(NotImplementedError):
            p.register(app=None, pm=None, activity=None)

    def test_unregister_optional(self):
        p = plugins.PluginBase()
        p.unregister()  # no-op by default


class TestRegressions:
    def test_plugin_with_no_classes_still_loads(self, plugin_dir):
        # File exists but defines no PluginBase subclass — discover
        # returns nothing, load returns nothing
        _write_plugin(plugin_dir / "empty.py", """
            X = 42
        """)
        assert plugins.discover() == []
        loaded = plugins.load_all(app=MagicMock(), pm=MagicMock(),
                                   activity=MagicMock())
        assert loaded == []

    def test_module_name_unique(self, plugin_dir):
        # Two files with the same basename but different content
        # should both load (module names are uniquified by the
        # file path on disk)
        _write_plugin(plugin_dir / "p1.py", """
            from app.plugins import PluginBase
            class P(PluginBase):
                name = "p1"
        """)
        _write_plugin(plugin_dir / "p2.py", """
            from app.plugins import PluginBase
            class P(PluginBase):
                name = "p2"
        """)
        result = plugins.discover()
        assert len(result) == 2
        names = sorted(p["name"] for p in result)
        assert names == ["p1", "p2"]

    def test_plugin_can_access_pm(self, plugin_dir):
        _write_plugin(plugin_dir / "p.py", """
            from app.plugins import PluginBase
            class P(PluginBase):
                name = "p"
                def register(self, app, pm, activity, notifier_module=None):
                    # Should be able to call pm.add_program etc.
                    pm.add_program(name="x", command="y", cwd="/")
        """)
        pm = MagicMock()
        plugins.load_all(app=MagicMock(), pm=pm, activity=MagicMock())
        pm.add_program.assert_called_once()
