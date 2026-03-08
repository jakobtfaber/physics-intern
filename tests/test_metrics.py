"""Tests for metrics tracking."""

from sciralph.metrics import MetricsTracker


def test_record_call():
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False)
    assert m.total_input_tokens == 1000
    assert m.total_output_tokens == 500
    assert len(m.calls) == 1


def test_max_tokens_tracking():
    m = MetricsTracker()
    m.record_call(1, "researcher", 1000, 500, 2.5, True)
    assert m.max_tokens_reached_count == 1


def test_critic_iteration_tracking():
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 100, 50, 1.0, False)
    m.record_call(5, "deep_critic", 200, 100, 3.0, False)
    assert m.last_critic_iteration == 5


def test_alert():
    m = MetricsTracker()
    m.alert(3, "test alert message")
    assert len(m.alerts) == 1
    assert m.alerts[0]["iteration"] == 3


def test_to_markdown():
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False)
    m.record_call(2, "researcher", 2000, 1000, 5.0, True)
    m.alert(2, "max_tokens hit")
    md = m.to_markdown(
        file_sizes={"RESEARCH_STATE.md": 5000},
        thresholds={"RESEARCH_STATE.md": 50000},
    )
    assert "---" in md
    assert "total_llm_calls: 2" in md
    assert "orchestrator" in md
    assert "max_tokens hit" in md
    assert "RESEARCH_STATE.md" in md
