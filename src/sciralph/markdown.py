"""Backward-compat shim — real module is utils.markdown."""
# Re-export everything including private names used by tests.
import importlib as _importlib
import sys as _sys

_real = _importlib.import_module(".utils.markdown", __package__)

# Copy all attributes so `from sciralph.markdown import _foo` works.
_globals = globals()
for _name in dir(_real):
    if not _name.startswith("__"):
        _globals[_name] = getattr(_real, _name)
