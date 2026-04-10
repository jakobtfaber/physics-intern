"""Tests for the one-shot runner (open_dirac.one_shot.runner)."""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from open_dirac.one_shot.runner import (
    _is_transient,
    _resolve_ground_truth,
    build_user_message,
)


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


# ---------------------------------------------------------------------------
# _resolve_ground_truth
# ---------------------------------------------------------------------------

class TestResolveGroundTruth:

    def test_answer_in_problem_def(self, tmp_path):
        problem_def = {"problem": "Compute X", "answer": "42"}
        result = _resolve_ground_truth(problem_def, tmp_path / "problem.yaml")
        assert result == {"problem_def": problem_def}

    def test_numeric_answer(self, tmp_path):
        problem_def = {"problem": "Compute X", "answer": 3.14}
        result = _resolve_ground_truth(problem_def, tmp_path / "problem.yaml")
        assert result is not None
        assert result["problem_def"]["answer"] == 3.14

    def test_empty_string_answer_falls_through(self, tmp_path):
        """Empty string answer is not treated as having a ground truth."""
        problem_def = {"problem": "Compute X", "answer": "  "}
        with patch("open_dirac.one_shot.runner.load_reference_file", return_value=(None, None)):
            result = _resolve_ground_truth(problem_def, tmp_path / "problem.yaml")
        assert result is None

    def test_no_answer_no_reference(self, tmp_path):
        problem_def = {"problem": "Compute X"}
        with patch("open_dirac.one_shot.runner.load_reference_file", return_value=(None, None)):
            result = _resolve_ground_truth(problem_def, tmp_path / "problem.yaml")
        assert result is None

    def test_reference_file_with_def_answer(self, tmp_path):
        problem_def = {"problem": "Compute X"}
        ref_code = "def answer():\n    return 42"
        with patch("open_dirac.one_shot.runner.load_reference_file", return_value=(ref_code, None)):
            result = _resolve_ground_truth(problem_def, tmp_path / "problem.yaml")
        assert result is not None
        assert result["reference_code"] == ref_code

    def test_reference_file_expression(self, tmp_path):
        """Legacy expression-style reference is patched into problem_def."""
        problem_def = {"problem": "Compute X"}
        with patch("open_dirac.one_shot.runner.load_reference_file", return_value=("pi/4", None)):
            result = _resolve_ground_truth(problem_def, tmp_path / "problem.yaml")
        assert result is not None
        assert result["problem_def"]["answer"] == "pi/4"
        # Original problem_def should not be mutated
        assert "answer" not in problem_def
