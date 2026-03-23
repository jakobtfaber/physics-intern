"""Tests for shared JSON parsing utilities in sciralph.agents.parsing."""

import json

import pytest

from sciralph.agents.parsing import fix_invalid_json_escapes, try_json_loads


# ---------------------------------------------------------------------------
# fix_invalid_json_escapes
# ---------------------------------------------------------------------------


class TestFixInvalidJsonEscapes:
    def test_valid_double_backslash_preserved(self):
        r"""\\epsilon (already valid \\) should be preserved unchanged."""
        s = r'{"x": "\\epsilon"}'
        assert fix_invalid_json_escapes(s) == s

    def test_lone_backslash_doubled(self):
        r"""\( (lone backslash) should become \\(."""
        s = r'{"x": "\("}'
        fixed = fix_invalid_json_escapes(s)
        assert fixed == r'{"x": "\\("}'

    def test_mixed_escaping(self):
        r"""Mixed \(\\delta\) → \\(\\delta\\)."""
        s = r'{"x": "\(\\delta\)"}'
        fixed = fix_invalid_json_escapes(s)
        assert fixed == r'{"x": "\\(\\delta\\)"}'

    def test_valid_double_backslash_b(self):
        r"""\\bar — first letter 'b' is in valid set but \\b is valid JSON
        escape (backspace). The \\ pair should be preserved as-is."""
        s = r'{"x": "\\bar"}'
        assert fix_invalid_json_escapes(s) == s

    def test_valid_double_backslash_h(self):
        r"""\\hat — first letter 'h' NOT in valid set. But since \\ is a
        valid pair, it should be preserved unchanged (the regression case)."""
        s = r'{"x": "\\hat"}'
        assert fix_invalid_json_escapes(s) == s

    def test_standard_json_escapes_untouched(self):
        r"""Standard JSON escapes like \n, \t, \", \\ should pass through."""
        s = r'{"x": "line1\nline2\ttab\"quote\\"}'
        assert fix_invalid_json_escapes(s) == s

    def test_unicode_escape_untouched(self):
        r"""\u0041 should pass through unchanged."""
        s = r'{"x": "\u0041"}'
        assert fix_invalid_json_escapes(s) == s

    def test_latex_frac(self):
        r"""\frac should become \\frac (lone backslash, f not in valid set)."""
        s = r'{"x": "\frac{1}{2}"}'
        fixed = fix_invalid_json_escapes(s)
        # \f is a valid JSON escape (form feed), so it should NOT be doubled
        assert fixed == s

    def test_latex_delta(self):
        r"""\delta — lone backslash, d not in valid set → \\delta."""
        s = r'{"x": "\delta"}'
        fixed = fix_invalid_json_escapes(s)
        assert fixed == r'{"x": "\\delta"}'

    def test_no_backslashes(self):
        """String without backslashes should be unchanged."""
        s = '{"x": "hello world"}'
        assert fix_invalid_json_escapes(s) == s


# ---------------------------------------------------------------------------
# try_json_loads
# ---------------------------------------------------------------------------


class TestTryJsonLoads:
    def test_valid_json_parses_directly(self):
        s = '{"result": "T_H = 1/(8*pi*M)", "confidence": "exact"}'
        parsed = try_json_loads(s)
        assert parsed["result"] == "T_H = 1/(8*pi*M)"

    def test_json_with_lone_backslash_fixed_and_parsed(self):
        r"""JSON with \( should be fixed and parsed."""
        s = r'{"result": "T_H = \(1/(8\pi M)\)", "confidence": "exact"}'
        parsed = try_json_loads(s)
        assert "T_H" in parsed["result"]
        assert parsed["confidence"] == "exact"

    def test_json_with_mixed_escaping_parsed(self):
        r"""JSON with mixed \\epsilon and \delta should be parsed."""
        s = r'{"result": "\\epsilon + \delta", "confidence": "approximate"}'
        parsed = try_json_loads(s)
        assert "epsilon" in parsed["result"]
        assert "delta" in parsed["result"]

    def test_truly_invalid_json_raises(self):
        """Truly invalid JSON (not just escape issues) should raise."""
        s = '{"result": broken}'
        with pytest.raises((json.JSONDecodeError, ValueError)):
            try_json_loads(s)

    def test_already_valid_json_with_backslash_n(self):
        """Valid JSON with \\n should parse without modification."""
        s = '{"x": "line1\\nline2"}'
        parsed = try_json_loads(s)
        assert parsed["x"] == "line1\nline2"
