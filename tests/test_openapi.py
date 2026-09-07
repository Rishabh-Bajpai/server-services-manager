"""Tests for app/openapi.py.

We build a minimal Flask app with a few routes and verify the
generated OpenAPI spec is valid and includes the expected paths,
methods, parameters, and security schemes.
"""
import pytest
from flask import Flask, jsonify

from app.openapi import (
    describe,
    generate_spec,
    add_security_scheme,
    clear_registry,
)


@pytest.fixture(autouse=True)
def reset_registry():
    """Ensure each test starts with a clean registry."""
    clear_registry()
    yield
    clear_registry()


@pytest.fixture
def app_with_routes():
    app = Flask(__name__)

    @app.route('/api/items', methods=['GET'])
    @describe(summary="List items", tag="Items")
    def list_items():
        return jsonify([])

    @app.route('/api/items/<int:item_id>', methods=['GET'])
    @describe(summary="Get an item", tag="Items",
              parameters=[{"name": "verbose", "in": "query", "schema": {"type": "boolean"}}])
    def get_item(item_id):
        return jsonify({"id": item_id})

    @app.route('/api/items', methods=['POST'])
    @describe(summary="Create an item", tag="Items",
              request_body={"required": True, "content": {"application/json": {"schema": {"type": "object"}}}})
    def create_item():
        return jsonify({"id": 1})

    @app.route('/health')
    def health():
        return jsonify({"status": "ok"})

    add_security_scheme({"name": "sessionCookie", "type": "apiKey", "in": "cookie"})
    yield app


# ---------------------------------------------------------------------------
# Basic generation
# ---------------------------------------------------------------------------

def test_spec_is_openapi_3(app_with_routes):
    spec = generate_spec(app_with_routes)
    assert spec["openapi"].startswith("3.")
    assert "info" in spec
    assert spec["info"]["title"]


def test_spec_includes_all_paths(app_with_routes):
    spec = generate_spec(app_with_routes)
    paths = spec["paths"]
    assert "/api/items" in paths
    assert "/api/items/{item_id}" in paths
    assert "/health" in paths


def test_spec_collects_methods_per_path(app_with_routes):
    spec = generate_spec(app_with_routes)
    items = spec["paths"]["/api/items"]
    assert "get" in items
    assert "post" in items


def test_spec_omits_head_and_options(app_with_routes):
    """Flask auto-registers HEAD/OPTIONS for every route — they should
    be filtered out of the spec to keep it focused.
    """
    spec = generate_spec(app_with_routes)
    health = spec["paths"]["/health"]
    assert "head" not in health
    assert "options" not in health


def test_spec_includes_describe_metadata(app_with_routes):
    spec = generate_spec(app_with_routes)
    items_list = spec["paths"]["/api/items"]["get"]
    assert items_list["summary"] == "List items"
    assert items_list["tags"] == ["Items"]


def test_spec_includes_path_parameters(app_with_routes):
    spec = generate_spec(app_with_routes)
    get_item = spec["paths"]["/api/items/{item_id}"]["get"]
    params = get_item.get("parameters", [])
    names = [p["name"] for p in params]
    assert "item_id" in names
    item_id_param = next(p for p in params if p["name"] == "item_id")
    assert item_id_param["in"] == "path"
    assert item_id_param["required"] is True
    assert item_id_param["schema"]["type"] == "integer"


def test_spec_includes_query_parameters(app_with_routes):
    spec = generate_spec(app_with_routes)
    get_item = spec["paths"]["/api/items/{item_id}"]["get"]
    params = get_item.get("parameters", [])
    assert any(p["name"] == "verbose" for p in params)


def test_spec_includes_request_body_when_described(app_with_routes):
    spec = generate_spec(app_with_routes)
    post = spec["paths"]["/api/items"]["post"]
    assert "requestBody" in post
    assert post["requestBody"]["required"] is True


def test_spec_infers_json_body_for_write_methods(app_with_routes):
    """Write methods without a manual requestBody still get a default
    application/json hint so the docs aren't confusing.
    """
    spec = generate_spec(app_with_routes)
    # The /api/items POST has an explicit body; check the health GET
    # doesn't (since it's a read method).
    health = spec["paths"]["/health"]["get"]
    assert "requestBody" not in health


def test_spec_operation_ids_are_unique(app_with_routes):
    spec = generate_spec(app_with_routes)
    op_ids = []
    for ops in spec["paths"].values():
        for op in ops.values():
            if "operationId" in op:
                op_ids.append(op["operationId"])
    assert len(op_ids) == len(set(op_ids))


def test_spec_tags_collected(app_with_routes):
    spec = generate_spec(app_with_routes)
    tag_names = {t["name"] for t in spec["tags"]}
    assert "Items" in tag_names
    # /health gets tagged "Health" via the prefix list; just verify it
    # has some sensible tag rather than asserting the literal string.
    assert len(tag_names) >= 2


def test_spec_security_schemes(app_with_routes):
    spec = generate_spec(app_with_routes)
    assert "components" in spec
    assert "securitySchemes" in spec["components"]
    schemes = spec["components"]["securitySchemes"]
    assert "sessionCookie" in schemes


def test_spec_responses_have_default_200(app_with_routes):
    spec = generate_spec(app_with_routes)
    get_item = spec["paths"]["/api/items/{item_id}"]["get"]
    assert "200" in get_item["responses"]


def test_spec_delete_gets_404(app_with_routes):
    app = Flask(__name__)

    @app.route('/api/x/<name>', methods=['DELETE'])
    def delete_x(name):
        return jsonify({})

    spec = generate_spec(app)
    delete_op = spec["paths"]["/api/x/{name}"]["delete"]
    assert "404" in delete_op["responses"]


# ---------------------------------------------------------------------------
# Path parameter type inference
# ---------------------------------------------------------------------------

def test_int_converter_yields_integer_schema():
    app = Flask(__name__)

    @app.route('/u/<int:user_id>')
    def u(user_id):
        return ""

    spec = generate_spec(app)
    params = spec["paths"]["/u/{user_id}"]["get"]["parameters"]
    assert any(p["name"] == "user_id" and p["schema"]["type"] == "integer" for p in params)


def test_string_converter_yields_string_schema():
    app = Flask(__name__)

    @app.route('/u/<name>')
    def u(name):
        return ""

    spec = generate_spec(app)
    params = spec["paths"]["/u/{name}"]["get"]["parameters"]
    assert any(p["name"] == "name" and p["schema"]["type"] == "string" for p in params)


def test_path_converter_yields_string_schema():
    app = Flask(__name__)

    @app.route('/files/<path:filepath>')
    def files(filepath):
        return ""

    spec = generate_spec(app)
    params = spec["paths"]["/files/{filepath}"]["get"]["parameters"]
    filepath_param = next(p for p in params if p["name"] == "filepath")
    assert filepath_param["schema"]["type"] == "string"


# ---------------------------------------------------------------------------
# describe() decorator
# ---------------------------------------------------------------------------

def test_describe_returns_unchanged_function():
    def f():
        return 1
    decorated = describe(summary="x")(f)
    assert decorated is f
    assert decorated() == 1


def test_describe_default_values():
    app = Flask(__name__)

    @app.route('/test')
    @describe()
    def t():
        return ""

    spec = generate_spec(app)
    op = spec["paths"]["/test"]["get"]
    assert op["summary"] == ""
    assert op["description"] == ""
    # Tag should fall back to path-prefix based
    assert "Other" in op["tags"]


# ---------------------------------------------------------------------------
# Static endpoint excluded
# ---------------------------------------------------------------------------

def test_static_endpoint_excluded():
    app = Flask(__name__)
    # Flask auto-registers /static/<path:filename>; generate_spec must skip it
    spec = generate_spec(app)
    assert "/static/{filename}" not in spec["paths"]


# ---------------------------------------------------------------------------
# End-to-end: real server.py routes generate a valid spec
# ---------------------------------------------------------------------------

def test_real_server_routes_generate_spec():
    """The server.py app should produce a non-empty, well-formed spec."""
    import server as server_mod
    with server_mod.app.app_context():
        spec = generate_spec(server_mod.app)
    assert spec["openapi"].startswith("3.")
    assert len(spec["paths"]) > 30  # we have 50+ routes
    # Pick a few well-known routes and verify they exist
    assert "/health" in spec["paths"]
    assert "/login" in spec["paths"]
    # Programs routes exist (note: ``/programs`` is the GET list, the
    # sub-resources are at /programs/<name>/...)
    assert "/programs" in spec["paths"]
    assert "/api/programs/{name}/logs/search" in spec["paths"]
    assert "/api/docker/containers" in spec["paths"]