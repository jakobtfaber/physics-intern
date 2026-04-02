"""Backward-compat shim — real modules are rendering.snapshots and rendering.contexts."""
import importlib as _importlib

_real = _importlib.import_module(".rendering", __package__)

# Copy all attributes so `from sciralph.renderers import _problem_guidelines` works.
_globals = globals()
for _name in dir(_real):
    if not _name.startswith("__"):
        _globals[_name] = getattr(_real, _name)
