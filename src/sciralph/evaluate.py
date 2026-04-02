"""Backward-compat shim — real module is verification.evaluate."""
import importlib as _importlib

_real = _importlib.import_module(".verification.evaluate", __package__)

_globals = globals()
for _name in dir(_real):
    if not _name.startswith("__"):
        _globals[_name] = getattr(_real, _name)
