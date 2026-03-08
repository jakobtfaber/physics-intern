"""Tests for sandboxed Python execution."""

import pytest
import tempfile
from pathlib import Path
from sciralph.sandbox import execute_python


def test_execute_simple_script():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("print('hello world')\n")
        f.flush()
        result = execute_python(f.name)
    assert result.stdout.strip() == "hello world"
    assert result.returncode == 0
    assert not result.timed_out


def test_execute_script_with_error():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("raise ValueError('test error')\n")
        f.flush()
        result = execute_python(f.name)
    assert result.returncode != 0
    assert "ValueError" in result.stderr


def test_execute_timeout():
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False) as f:
        f.write("import time; time.sleep(10)\n")
        f.flush()
        result = execute_python(f.name, timeout=1)
    assert result.timed_out
    assert result.returncode == -1


def test_execute_nonexistent_script():
    result = execute_python("/nonexistent/script.py")
    assert result.returncode != 0
