from app.palette import (
    PaletteItem, _score, search, build_palette_index,
)


class TestScore:
    def test_empty_needle_returns_zero(self):
        assert _score("", "hello") == 0

    def test_not_subsequence(self):
        assert _score("xyz", "abc") == 0

    def test_basic_match(self):
        s = _score("hi", "history")
        assert s > 0

    def test_case_insensitive(self):
        a = _score("foo", "FOOBAR")
        b = _score("FOO", "foobar")
        assert a == b

    def test_boundary_bonus(self):
        # "cr" at word boundary should score higher than "cr" mid-word
        boundary = _score("cr", "cron jobs")
        middle = _score("cr", "secret")
        assert boundary > middle

    def test_exact_prefix_bonus(self):
        prefix = _score("foo", "foobar")
        nonprefix = _score("foo", "xfoobar")
        assert prefix > nonprefix

    def test_shorter_haystack_bonus(self):
        short = _score("foo", "foobar")
        long = _score("foo", "the quick brown foobar")
        assert short > long


class TestSearch:
    def _items(self):
        return [
            PaletteItem("page", "dashboard", "Dashboard", "Main service dashboard"),
            PaletteItem("page", "monitor", "System Monitor", "Real-time charts"),
            PaletteItem("program", "api", "API server", "My API service"),
            PaletteItem("program", "web", "Web frontend", "Public website"),
        ]

    def test_empty_query_returns_initial(self):
        results = search(self._items(), "")
        assert len(results) == 4

    def test_query_ranked(self):
        results = search(self._items(), "api")
        # The "API server" should rank first
        assert results[0]["id"] == "api"

    def test_query_finds_pages(self):
        results = search(self._items(), "monitor")
        assert results[0]["id"] == "monitor"

    def test_query_finds_by_description(self):
        results = search(self._items(), "charts")
        assert results[0]["id"] == "monitor"

    def test_limit(self):
        results = search(self._items(), "a", limit=1)
        assert len(results) == 1

    def test_no_match_returns_empty(self):
        results = search(self._items(), "xyzqwerty")
        assert results == []


class TestBuildPaletteIndex:
    def test_includes_pages(self):
        items = build_palette_index(lambda: [], lambda: [], lambda: [])
        kinds = {i.kind for i in items}
        assert "page" in kinds
        # Should have at least dashboard, monitor, control, etc.
        ids = [i.id for i in items]
        assert "dashboard" in ids

    def test_includes_programs(self):
        from app.process_manager import Program, ProgramConfig
        p = Program(ProgramConfig(name="api", command="x", cwd="/tmp"))
        items = build_palette_index(lambda: [p], lambda: [], lambda: [])
        # Should have api:start, api:stop, api:restart
        ids = [i.id for i in items]
        assert "api:start" in ids
        assert "api:stop" in ids
        assert "api:restart" in ids

    def test_includes_system_units(self):
        from types import SimpleNamespace
        u = SimpleNamespace(name="cron.service", description="cron daemon")
        items = build_palette_index(lambda: [], lambda: [], lambda: [u])
        ids = [i.id for i in items]
        assert "cron.service:start" in ids
        assert "cron.service:stop" in ids

    def test_includes_control_commands(self):
        cmds = [
            {"id": "deploy", "name": "Deploy", "description": "Run deploy"},
        ]
        items = build_palette_index(lambda: [], lambda: cmds, lambda: [])
        ids = [i.id for i in items]
        assert "deploy" in ids

    def test_run_dict_is_propagated(self):
        from app.process_manager import Program, ProgramConfig
        p = Program(ProgramConfig(name="api", command="x", cwd="/tmp"))
        items = build_palette_index(lambda: [p], lambda: [], lambda: [])
        start = next(i for i in items if i.id == "api:start")
        assert start.run["type"] == "action"
        assert start.run["url"] == "/programs/api/start"
        assert start.run["method"] == "POST"
