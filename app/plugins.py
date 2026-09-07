"""Plugin loader for user-supplied drop-in Python modules.

Plugins live in ``~/.server-services-manager/plugins/`` (one .py
file per plugin). The loader imports each one and looks for
plugin classes — a class with a ``name`` attribute that is a
subclass of :class:`PluginBase`. Each plugin is constructed and
its ``register()`` is called with the Flask app, the
:class:`~app.process_manager.ProcessManager`, and the activity
logger so it can do whatever it wants (mount routes, subscribe
to events, spawn helper threads).

Plugins are loaded once at startup. They are NOT auto-reloaded
on file change — too dangerous (an attacker who can write into
your home dir could replace a plugin and the manager would
silently load it on the next reload). A restart is required to
pick up new / changed plugins.
"""
import importlib.util
import inspect
import logging
import os
import sys
import traceback
from typing import List, Optional

logger = logging.getLogger("Plugins")

DEFAULT_DIR = os.path.expanduser("~/.server-services-manager/plugins")


class PluginBase:
    """Subclass and override register() to add a plugin."""

    name: str = "unnamed"
    description: str = ""
    version: str = "0.0.0"

    def register(self, app, pm, activity, notifier_module=None) -> None:
        """Hook called once at startup.

        Parameters
        ----------
        app : Flask
            The Flask application; mount routes / blueprints here.
        pm : ProcessManager
            The managed-program process manager; add/edit/delete
            programs programmatically.
        activity : module
            The activity log module (``app.activity``); call
            ``activity.log(...)`` to record events.
        notifier_module : module, optional
            The ``app.notifier`` module, exposed so plugins can
            reuse the existing notifier infrastructure (build
            an :class:`~app.notifier.Event`, call
            :func:`~app.notifier.fanout`).
        """
        raise NotImplementedError

    def unregister(self) -> None:
        """Optional cleanup hook called at shutdown."""


def _iter_plugin_files(directory: str):
    if not os.path.isdir(directory):
        return
    for entry in sorted(os.listdir(directory)):
        if not entry.endswith(".py"):
            continue
        if entry.startswith("_") or entry.startswith("."):
            continue
        path = os.path.join(directory, entry)
        if not os.path.isfile(path):
            continue
        yield entry, path


def _load_module(name: str, path: str):
    """Import a Python file as a module with a unique name."""
    module_name = f"ssm_plugin_{name[:-3]}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as e:
        logger.error(f"plugin {name} failed to import: {e}")
        logger.error(traceback.format_exc())
        sys.modules.pop(module_name, None)
        return None
    return module


def _find_plugin_classes(module):
    """Return concrete subclasses of PluginBase defined in module."""
    out = []
    for _, obj in inspect.getmembers(module, inspect.isclass):
        if obj is PluginBase:
            continue
        if not issubclass(obj, PluginBase):
            continue
        if obj.__module__ != module.__name__:
            continue  # re-exported; ignore
        out.append(obj)
    return out


def discover(directory: Optional[str] = None) -> List[dict]:
    """Discover available plugins without instantiating them.

    Returns a list of {name, file, classes: [{class_name, name,
    description, version}]}. Safe to call from the UI to show
    what plugins are installed but not yet loaded.
    """
    if directory is None:
        directory = DEFAULT_DIR
    out = []
    for fname, fpath in _iter_plugin_files(directory):
        module = _load_module(fname, fpath)
        if module is None:
            out.append({"file": fname, "error": "import failed", "classes": []})
            continue
        for cls in _find_plugin_classes(module):
            out.append({
                "file": fname,
                "class": cls.__name__,
                "name": getattr(cls, "name", cls.__name__),
                "description": getattr(cls, "description", ""),
                "version": getattr(cls, "version", "0.0.0"),
            })
    return out


def load_all(app=None, pm=None, activity=None,
             notifier_module=None,
             directory: Optional[str] = None) -> List[PluginBase]:
    """Discover + instantiate + register all plugins.

    Returns the list of successfully-instantiated PluginBase
    objects. Failures are logged but do not raise; one bad
    plugin must never break the rest of the manager.
    """
    if directory is None:
        directory = DEFAULT_DIR
    loaded: List[PluginBase] = []
    for fname, fpath in _iter_plugin_files(directory):
        module = _load_module(fname, fpath)
        if module is None:
            continue
        for cls in _find_plugin_classes(module):
            try:
                instance = cls()
            except Exception as e:
                logger.error(f"plugin {cls.__name__} in {fname} could not be instantiated: {e}")
                continue
            try:
                instance.register(
                    app=app, pm=pm, activity=activity,
                    notifier_module=notifier_module,
                )
                loaded.append(instance)
                logger.info(f"loaded plugin {instance.name} v{instance.version} from {fname}")
            except Exception as e:
                logger.error(f"plugin {instance.name}.register() failed: {e}")
                logger.error(traceback.format_exc())
    return loaded


def unload_all(plugins: List[PluginBase]) -> None:
    """Call unregister() on each plugin, ignoring errors."""
    for p in plugins:
        try:
            p.unregister()
        except Exception as e:
            logger.error(f"plugin {p.name}.unregister() failed: {e}")
