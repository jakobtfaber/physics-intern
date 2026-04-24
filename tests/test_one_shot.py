"""Tests for the one-shot runner (open_dirac.one_shot.runner)."""

from unittest.mock import MagicMock

import pytest

from open_dirac.one_shot.runner import build_user_message
from open_dirac.providers.retry import is_transient as _is_transient


# ---------------------------------------------------------------------------
# build_user_message
# ---------------------------------------------------------------------------

class TestBuildUserMessage:

    def test_plain_problem(self):
        msg = build_user_message("Derive the Hawking temperature.")
        assert msg == "Derive the Hawking temperature."

    def test_strips_whitespace(self):
        msg = build_user_message("  hello  \n")
        assert msg == "hello"

    def test_with_answer_template(self):
        msg = build_user_message("Problem text", "def answer():\n    return 42")
        assert "Problem text" in msg
        assert "```python" in msg
        assert "def answer():" in msg
        assert "Answer template" in msg

    def test_empty_template_ignored(self):
        msg = build_user_message("Problem text", "")
        assert "```python" not in msg
        assert "template" not in msg.lower()


# ---------------------------------------------------------------------------
# _is_transient
# ---------------------------------------------------------------------------

class TestIsTransient:

    def test_connection_error(self):
        assert _is_transient(ConnectionError("reset"))

    def test_timeout_error(self):
        assert _is_transient(TimeoutError("timed out"))

    def test_value_error_not_transient(self):
        assert not _is_transient(ValueError("bad input"))

    def test_runtime_error_not_transient(self):
        assert not _is_transient(RuntimeError("fail"))

    def test_status_code_429(self):
        exc = Exception("rate limit")
        exc.status_code = 429
        assert _is_transient(exc)

    def test_status_code_500(self):
        exc = Exception("server error")
        exc.status_code = 500
        assert _is_transient(exc)

    def test_status_code_503(self):
        exc = Exception("service unavailable")
        exc.status_code = 503
        assert _is_transient(exc)

    def test_status_code_400_not_transient(self):
        exc = Exception("bad request")
        exc.status_code = 400
        assert not _is_transient(exc)

    def test_response_status_code(self):
        """Extracts status from exc.response.status_code."""
        exc = Exception("error")
        exc.response = MagicMock(status_code=502)
        assert _is_transient(exc)

    def test_status_attribute(self):
        """Falls back to exc.status."""
        exc = Exception("error")
        exc.status = 504
        assert _is_transient(exc)
