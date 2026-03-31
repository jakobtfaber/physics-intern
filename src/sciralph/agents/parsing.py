"""Shared JSON parsing utilities for one-shot agents (researcher, reviewer, critic).

Handles LaTeX-contaminated JSON strings where LLMs emit invalid escape
sequences like ``\\epsilon``, ``\\frac``, or bare ``\\(`` inside JSON values.
"""

from __future__ import annotations

import json
import re

# Regex to find fenced ```json ... ``` blocks
JSON_FENCE_RE = re.compile(r"```json\s*\n(.*?)```", re.DOTALL)

# Null-byte sentinel used during two-pass escape fixing
_PLACEHOLDER = "\x00ESCAPED_BACKSLASH\x00"


def fix_invalid_json_escapes(s: str) -> str:
    r"""Fix invalid JSON escape sequences without corrupting valid ones.

    Two-pass approach:
    1. Protect already-valid ``\\X`` pairs by replacing ``\\`` with a placeholder.
    2. Double any remaining lone backslashes that aren't valid JSON escapes.
    3. Restore the placeholder back to ``\\``.

    This avoids the bug where a naive ``re.sub(r'\\(?![...])`` turns
    ``\\hat`` into ``\\\\hat`` (the ``\\h`` looks like an invalid escape
    because ``h`` is NOT in ``["\\/bfnrtu]``, but the *pair* ``\\`` is
    a valid JSON escape for a literal backslash).
    """
    # Step 1: protect valid \\ pairs
    protected = s.replace("\\\\", _PLACEHOLDER)
    # Step 2: fix lone backslashes that aren't valid JSON escapes
    fixed = re.sub(r'\\(?!["\\/bfnrtu])', r"\\\\", protected)
    # Step 2b: fix malformed \uXXXX sequences (\u not followed by 4 hex digits)
    fixed = re.sub(r"\\u(?![0-9a-fA-F]{4})", r"\\\\u", fixed)
    # Step 3: restore protected pairs
    return fixed.replace(_PLACEHOLDER, "\\\\")


def try_json_loads(s: str):
    """Parse JSON, retrying with escape fixes for LaTeX-contaminated strings.

    On initial failure, applies :func:`fix_invalid_json_escapes` and retries.
    Raises ``json.JSONDecodeError`` / ``ValueError`` if still invalid.
    """
    try:
        return json.loads(s)
    except (json.JSONDecodeError, ValueError):
        fixed = fix_invalid_json_escapes(s)
        if fixed != s:
            return json.loads(fixed)  # let caller handle if still bad
        raise
