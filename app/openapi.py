"""OpenAPI 3.0 spec generator for the Server Services Manager API.

Approach
--------
Walking ``app.url_map`` gives us every registered route (path + method)
automatically. For each route, we layer on:

- a short summary and longer description (registered via :func:`describe`)
- optional tag for grouping in the docs UI
- response schemas when the route returns JSON

Routes without a description still appear in the spec (so the docs are
complete), just with empty ``summary``/``description`` so the operator
knows to fill them in later.

Usage
-----
::

    from app.openapi import describe

    @app.route('/api/example', methods=['GET'])
    @describe(summary="List examples", tag="examples")
    def list_examples():
        return jsonify([])

The :func:`generate_spec` function returns a complete OpenAPI 3.0 dict
ready to serve at ``/openapi.json``.
"""
import logging
from typing import Callable, Dict, List, Optional

logger = logging.getLogger("OpenAPI")


# ---------------------------------------------------------------------------
# Description registry: keyed by (rule_path, methods_tuple)
# ---------------------------------------------------------------------------

_REGISTRY: Dict[tuple, dict] = {}
_SECURITY_SCHEMES: List[dict] = []


def describe(
    summary: str = "",
    description: str = "",
    tag: str = "",
    responses: Optional[dict] = None,
    parameters: Optional[list] = None,
    request_body: Optional[dict] = None,
) -> Callable:
    """Decorator to attach OpenAPI metadata to a Flask view function.

    The decorator is keyed on the function itself (Python identity),
    not the route path, so it survives blueprint re-registration.
    """

    def wrap(fn: Callable) -> Callable:
        _REGISTRY[id(fn)] = {
            "summary": summary,
            "description": description,
            "tag": tag,
            "responses": responses or {},
            "parameters": parameters or [],
            "request_body": request_body,
        }
        return fn

    return wrap


def add_security_scheme(scheme: dict) -> None:
    """Register a security scheme (e.g. cookie-based session auth)."""
    _SECURITY_SCHEMES.append(scheme)


# ---------------------------------------------------------------------------
# Path parameter inference from Flask route variables
# ---------------------------------------------------------------------------

_PATH_PARAM_TYPES = {
    "int": "integer",
    "float": "number",
    "string": "string",
    "path": "string",
    "uuid": "string",
}


# Map converter class names to their Flask URL syntax prefix.
_CONVERTER_CLASS_TO_NAME = {
    "IntegerConverter": "int",
    "FloatConverter": "float",
    "StringConverter": "string",
    "PathConverter": "path",
    "UUIDConverter": "uuid",
    "AnyConverter": "",  # <arg> without a prefix
}


def _path_parameters(rule) -> List[dict]:
    """Build OpenAPI ``parameters`` list from Flask's URL converters."""
    params = []
    for arg, converter in (rule._converters or {}).items():
        # Skip internal Flask variables
        if arg in ("static",):
            continue
        conv_name = _CONVERTER_CLASS_TO_NAME.get(type(converter).__name__, "")
        typ = _PATH_PARAM_TYPES.get(conv_name, "string")
        params.append({
            "name": arg,
            "in": "path",
            "required": True,
            "description": f"URL parameter ``{arg}``",
            "schema": {"type": typ},
        })
    return params


# ---------------------------------------------------------------------------
# Spec generation
# ---------------------------------------------------------------------------

_INFO = {
    "title": "Server Services Manager API",
    "description": (
        "REST API for managing programs, systemd units, Docker containers, "
        "cron jobs, firewall rules, packages, backups, and logs.\n\n"
        "Authentication: session cookie set by `POST /login`. The "
        "`GET /health` endpoint bypasses auth."
    ),
    "version": "1.0.0",
}


_TAGS_BY_PATH_PREFIX = [
    ("/api/programs", "Programs"),
    ("/api/system-services", "System Services"),
    ("/api/docker", "Docker"),
    ("/api/packages", "Packages"),
    ("/api/firewall", "Firewall"),
    ("/api/backups", "Backups"),
    ("/api/alerts", "Alerts"),
    ("/api/notifications", "Notifications"),
    ("/api/cron", "Cron"),
    ("/api/activity", "Activity"),
    ("/api/palette", "Command Palette"),
    ("/api/plugins", "Plugins"),
    ("/api/files", "Files"),
    ("/api/control", "Control Panel"),
    ("/api/health", "Health"),
    ("/programs", "Programs"),
    ("/system-services", "System Services"),
    ("/docker", "Docker"),
    ("/cron", "Cron"),
    ("/notifications", "Notifications"),
    ("/alerts", "Alerts"),
    ("/packages", "Packages"),
    ("/firewall", "Firewall"),
    ("/backups", "Backups"),
    ("/logs", "Logs"),
    ("/monitor", "Monitor"),
    ("/control", "Control Panel"),
    ("/activity", "Activity"),
    ("/login", "Auth"),
    ("/logout", "Auth"),
    ("/health", "Health"),
    ("/", "Dashboard"),
]


def _tag_for_path(path: str) -> str:
    for prefix, tag in _TAGS_BY_PATH_PREFIX:
        if path == prefix or path.startswith(prefix + "/") or path == prefix.rstrip("/"):
            return tag
    return "Other"


# Map Flask HTTP methods to OpenAPI method keys
_METHOD_KEYS = {"GET", "POST", "PUT", "DELETE", "PATCH", "OPTIONS", "HEAD"}


def _operation_id(path: str, method: str, view_fn: Callable) -> str:
    """Build a stable, unique operationId from path+method+view."""
    name = getattr(view_fn, "__name__", "anonymous")
    return f"{method.lower()}_{name}"


def generate_spec(flask_app) -> dict:
    """Build a complete OpenAPI 3.0 spec from the Flask app's routes."""
    paths: Dict[str, dict] = {}
    seen = set()  # track (endpoint, method) to avoid duplicates

    for rule in flask_app.url_map.iter_rules():
        if rule.endpoint == "static":
            continue
        view_fn = flask_app.view_functions.get(rule.endpoint)
        if view_fn is None:
            continue
        # Path with Flask variables translated to OpenAPI {var} form
        openapi_path = rule.rule
        for arg, conv in (rule._converters or {}).items():
            if arg == "static":
                continue
            conv_name = _CONVERTER_CLASS_TO_NAME.get(type(conv).__name__, "")
            # Replace `<int:arg>` → `{arg}`, `<arg>` → `{arg}`, etc.
            if conv_name:
                openapi_path = openapi_path.replace(f"<{conv_name}:{arg}>", f"{{{arg}}}")
            openapi_path = openapi_path.replace(f"<{arg}>", f"{{{arg}}}")

        # Flask rule.methods is a set; ignore HEAD/OPTIONS (auto-handled)
        methods = sorted(m for m in rule.methods if m in _METHOD_KEYS and m not in ("HEAD", "OPTIONS"))

        if openapi_path not in paths:
            paths[openapi_path] = {}

        path_params = _path_parameters(rule)
        meta = _REGISTRY.get(id(view_fn), {})
        tag = meta.get("tag") or _tag_for_path(openapi_path)
        summary = meta.get("summary") or ""
        description = meta.get("description") or ""

        for method in methods:
            op_key = (rule.endpoint, method)
            if op_key in seen:
                continue
            seen.add(op_key)

            op: dict = {
                "summary": summary,
                "description": description,
                "operationId": _operation_id(openapi_path, method, view_fn),
                "tags": [tag],
                "responses": meta.get("responses") or {
                    "200": {"description": "OK"},
                    "401": {"description": "Authentication required"},
                    "500": {"description": "Server error"},
                },
            }
            if path_params:
                op["parameters"] = path_params + list(meta.get("parameters") or [])
            elif meta.get("parameters"):
                op["parameters"] = list(meta["parameters"])
            if meta.get("request_body"):
                op["requestBody"] = meta["request_body"]
            elif method in ("POST", "PUT", "PATCH", "DELETE"):
                # Hint that body is JSON for write methods
                op["requestBody"] = {
                    "required": False,
                    "content": {"application/json": {"schema": {"type": "object"}}},
                }
            if method == "DELETE":
                op["responses"]["404"] = {"description": "Not found"}

            paths[openapi_path][method.lower()] = op

    spec = {
        "openapi": "3.0.3",
        "info": _INFO,
        "servers": [{"url": "/", "description": "Same origin"}],
        "paths": paths,
    }
    if _SECURITY_SCHEMES:
        spec["components"] = {"securitySchemes": {s["name"]: s for s in _SECURITY_SCHEMES}}
    # Tag list — collect unique tags from paths for the UI
    tags = sorted({op.get("tags", ["Other"])[0]
                   for ops in paths.values() for op in ops.values()})
    spec["tags"] = [{"name": t} for t in tags]
    return spec


# ---------------------------------------------------------------------------
# Convenience for tests
# ---------------------------------------------------------------------------

def clear_registry() -> None:
    """Reset the registry (used by tests to ensure isolation)."""
    _REGISTRY.clear()
    _SECURITY_SCHEMES.clear()