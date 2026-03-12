"""Tests for transient-error retry logic in llm.py."""

import time
from unittest.mock import MagicMock, patch

import pytest

from sciralph.config import Config
from sciralph.llm import _is_transient, _call_provider_with_retry
from sciralph.providers.base import ProviderResponse


# ---------------------------------------------------------------------------
# _is_transient classifier
# ---------------------------------------------------------------------------

class FakeHTTPError(Exception):
    def __init__(self, status_code):
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


class FakeStatusError(Exception):
    """Uses .status instead of .status_code (some SDKs)."""
    def __init__(self, status):
        self.status = status
        super().__init__(f"status {status}")


class FakeConnectionError(ConnectionError):
    pass


class FakeTimeoutError(TimeoutError):
    pass


class FakeReadTimeout(Exception):
    """Mimics requests.exceptions.ReadTimeout naming."""
    pass
FakeReadTimeout.__name__ = "ReadTimeout"


class FakeAuthError(Exception):
    def __init__(self):
        self.status_code = 401
        super().__init__("Unauthorized")


@pytest.mark.parametrize("exc", [
    FakeHTTPError(429),
    FakeHTTPError(502),
    FakeHTTPError(503),
    FakeHTTPError(504),
    FakeStatusError(429),
    FakeStatusError(504),
    FakeConnectionError("connection reset"),
    FakeTimeoutError("timed out"),
    FakeReadTimeout(),
    ConnectionError("reset by peer"),
    TimeoutError("deadline exceeded"),
])
def test_is_transient_true(exc):
    assert _is_transient(exc) is True


@pytest.mark.parametrize("exc", [
    FakeHTTPError(400),
    FakeHTTPError(401),
    FakeHTTPError(403),
    FakeHTTPError(404),
    FakeHTTPError(422),
    FakeAuthError(),
    ValueError("bad input"),
    RuntimeError("something else"),
    TypeError("wrong type"),
])
def test_is_transient_false(exc):
    assert _is_transient(exc) is False


# ---------------------------------------------------------------------------
# _call_provider_with_retry
# ---------------------------------------------------------------------------

def _make_provider_response(text="ok"):
    return ProviderResponse(
        text=text, input_tokens=10, output_tokens=5,
        stop_reason="end_turn",
    )


def _make_config(**overrides):
    defaults = dict(
        api_retry_max=3,
        api_retry_initial_delay=0.01,  # fast for tests
        api_retry_max_delay=0.1,
    )
    defaults.update(overrides)
    return Config(**defaults)


def test_retry_succeeds_after_transient_errors():
    """Provider fails twice with 503, then succeeds on third attempt."""
    provider = MagicMock()
    provider.call.side_effect = [
        FakeHTTPError(503),
        FakeHTTPError(503),
        _make_provider_response("success"),
    ]
    config = _make_config()

    with patch("sciralph.llm.time.sleep") as mock_sleep:
        result = _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert result.text == "success"
    assert provider.call.call_count == 3
    assert mock_sleep.call_count == 2


def test_retry_respects_backoff():
    """Verify exponential backoff with delay doubling."""
    provider = MagicMock()
    provider.call.side_effect = [
        FakeHTTPError(429),
        FakeHTTPError(429),
        FakeHTTPError(429),
        _make_provider_response("ok"),
    ]
    config = _make_config(api_retry_initial_delay=1.0, api_retry_max_delay=10.0)

    with patch("sciralph.llm.time.sleep") as mock_sleep:
        result = _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert result.text == "ok"
    # Delays: 1.0, 2.0, 4.0
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays == [1.0, 2.0, 4.0]


def test_retry_caps_at_max_delay():
    """Backoff delay is capped at api_retry_max_delay."""
    provider = MagicMock()
    provider.call.side_effect = [
        FakeHTTPError(504),
        FakeHTTPError(504),
        FakeHTTPError(504),
        _make_provider_response("ok"),
    ]
    config = _make_config(api_retry_initial_delay=5.0, api_retry_max_delay=8.0)

    with patch("sciralph.llm.time.sleep") as mock_sleep:
        _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    # Delays: min(5, 8)=5, min(10, 8)=8, min(20, 8)=8
    delays = [call.args[0] for call in mock_sleep.call_args_list]
    assert delays == [5.0, 8.0, 8.0]


def test_no_retry_on_non_transient_error():
    """Non-transient errors are raised immediately without retry."""
    provider = MagicMock()
    provider.call.side_effect = FakeHTTPError(400)
    config = _make_config()

    with pytest.raises(FakeHTTPError):
        _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert provider.call.call_count == 1


def test_exhausted_retries_raises():
    """After all retries are exhausted, the last transient error is raised."""
    provider = MagicMock()
    provider.call.side_effect = FakeHTTPError(503)
    config = _make_config(api_retry_max=2)

    with patch("sciralph.llm.time.sleep"):
        with pytest.raises(FakeHTTPError):
            _call_provider_with_retry(
                provider, config, model="m", max_tokens=100,
                system="s", messages=[],
            )

    # 1 initial + 2 retries = 3
    assert provider.call.call_count == 3


def test_no_retry_when_max_is_zero():
    """api_retry_max=0 means no retries at all."""
    provider = MagicMock()
    provider.call.side_effect = FakeHTTPError(503)
    config = _make_config(api_retry_max=0)

    with pytest.raises(FakeHTTPError):
        _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert provider.call.call_count == 1


def test_immediate_success_no_sleep():
    """When the first call succeeds, no sleep occurs."""
    provider = MagicMock()
    provider.call.return_value = _make_provider_response("instant")
    config = _make_config()

    with patch("sciralph.llm.time.sleep") as mock_sleep:
        result = _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert result.text == "instant"
    assert provider.call.call_count == 1
    mock_sleep.assert_not_called()


def test_retry_with_connection_error():
    """ConnectionError is retried."""
    provider = MagicMock()
    provider.call.side_effect = [
        ConnectionError("reset by peer"),
        _make_provider_response("recovered"),
    ]
    config = _make_config()

    with patch("sciralph.llm.time.sleep"):
        result = _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert result.text == "recovered"
    assert provider.call.call_count == 2


def test_retry_with_timeout_error():
    """TimeoutError is retried."""
    provider = MagicMock()
    provider.call.side_effect = [
        TimeoutError("deadline exceeded"),
        _make_provider_response("ok"),
    ]
    config = _make_config()

    with patch("sciralph.llm.time.sleep"):
        result = _call_provider_with_retry(
            provider, config, model="m", max_tokens=100,
            system="s", messages=[],
        )

    assert result.text == "ok"
