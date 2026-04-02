"""Backward-compat shim — real module is agents.computer.tools."""
from .tool_call import ToolCall  # noqa: F401 — widely imported from here

_tools_mod = None


def __getattr__(name: str):
    """Lazy import to avoid circular imports during package init."""
    global _tools_mod
    if _tools_mod is None:
        import importlib
        # Import the tools submodule directly — importlib with the full
        # dotted name avoids triggering agents/computer/__init__.py.
        _tools_mod = importlib.import_module("sciralph.agents.computer.tools")
    val = getattr(_tools_mod, name, None)
    if val is not None:
        return val
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
