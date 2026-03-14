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


def test_to_markdown_shows_all_calls():
    """Verify all calls appear in rendered markdown, not just the last 20."""
    m = MetricsTracker()
    for i in range(1, 31):
        m.record_call(i, f"agent_{i}", 100, 50, 1.0, False)
    md = m.to_markdown()
    for i in range(1, 31):
        assert f"agent_{i}" in md, f"agent_{i} missing from metrics markdown"


# --- Tool-use metrics tests ---

def test_record_call_with_tool_fields():
    """record_call accepts rounds, tool_calls, truncated kwargs."""
    m = MetricsTracker()
    m.record_call(1, "computationalist", 1000, 500, 3.0, False,
                  rounds=3, tool_calls=2, truncated=False)
    assert m.total_tool_calls == 2
    assert m.calls[0].rounds == 3
    assert m.calls[0].tool_calls == 2
    assert not m.calls[0].truncated


def test_tool_calls_cumulative():
    """total_tool_calls accumulates across calls."""
    m = MetricsTracker()
    m.record_call(1, "computationalist", 500, 200, 2.0, False, tool_calls=3)
    m.record_call(2, "computationalist", 600, 300, 2.5, False, tool_calls=2)
    assert m.total_tool_calls == 5


def test_tool_columns_shown_when_tool_calls_present():
    """Rounds + Tool Calls columns appear when any record has tool_calls > 0."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False)
    m.record_call(2, "computationalist", 2000, 1000, 5.0, False,
                  rounds=3, tool_calls=2)
    md = m.to_markdown()
    assert "Rounds" in md
    assert "Tool Calls" in md
    assert "total_tool_calls: 2" in md


def test_no_tool_columns_when_no_tool_calls():
    """Rounds/Tool Calls columns are hidden when no agent used tools."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False)
    m.record_call(2, "researcher", 2000, 1000, 5.0, False)
    md = m.to_markdown()
    assert "Rounds" not in md
    assert "Tool Calls" not in md
    assert "total_tool_calls" not in md


def test_default_tool_fields():
    """Default values for new fields are backward-compatible."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 100, 50, 1.0, False)
    assert m.calls[0].rounds == 1
    assert m.calls[0].tool_calls == 0
    assert not m.calls[0].truncated


# --- Reasoning token metrics tests ---

def test_reasoning_tokens_accumulate():
    """total_reasoning_tokens and total_answer_tokens accumulate correctly."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False,
                  reasoning_tokens=300, answer_tokens=200)
    m.record_call(2, "researcher", 2000, 800, 3.0, False,
                  reasoning_tokens=500, answer_tokens=300)
    assert m.total_reasoning_tokens == 800
    assert m.total_answer_tokens == 500


def test_reasoning_tokens_default_zero():
    """Reasoning tokens default to 0 for backward compatibility."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 100, 50, 1.0, False)
    assert m.total_reasoning_tokens == 0
    assert m.total_answer_tokens == 0
    assert m.calls[0].reasoning_tokens == 0
    assert m.calls[0].answer_tokens == 0


def test_reasoning_tokens_in_markdown():
    """Reasoning token totals appear in YAML frontmatter when > 0."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False,
                  reasoning_tokens=300, answer_tokens=200)
    md = m.to_markdown()
    assert "total_reasoning_tokens: 300" in md
    assert "total_answer_tokens: 200" in md


def test_no_reasoning_tokens_in_markdown_when_zero():
    """Reasoning token fields omitted from frontmatter when all zero."""
    m = MetricsTracker()
    m.record_call(1, "orchestrator", 1000, 500, 2.5, False)
    md = m.to_markdown()
    assert "total_reasoning_tokens" not in md
    assert "total_answer_tokens" not in md
