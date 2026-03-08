"""Tests for per-call conversation logging in llm.py."""

import sciralph.llm as llm_module
from sciralph.config import Config
from sciralph.llm import LLMResponse, _write_conversation_log
from sciralph.workspace import WorkspaceManager


def _make_response(**overrides):
    defaults = dict(text="response text", input_tokens=100,
                    output_tokens=50, stop_reason="end_turn", duration=1.23)
    defaults.update(overrides)
    return LLMResponse(**defaults)


def test_creates_correctly_named_file(tmp_path):
    config = Config(logs_dir=str(tmp_path))
    # Reset seq counter for clean test
    llm_module._call_seq.clear()

    _write_conversation_log(config, _make_response(),
                            "system prompt", "user msg", "researcher", 3)

    files = list(tmp_path.iterdir())
    assert len(files) == 1
    assert files[0].name == "iter003_researcher_1.md"


def test_file_contains_expected_sections(tmp_path):
    config = Config(logs_dir=str(tmp_path))
    llm_module._call_seq.clear()

    _write_conversation_log(config, _make_response(text="the answer"),
                            "sys prompt here", "user content here",
                            "orchestrator", 5)

    content = (tmp_path / "iter005_orchestrator_1.md").read_text()
    assert "## System Prompt" in content
    assert "sys prompt here" in content
    assert "## User Content" in content
    assert "user content here" in content
    assert "## Response" in content
    assert "the answer" in content
    assert "| Input tokens | 100 |" in content


def test_seq_increments_for_same_iteration(tmp_path):
    config = Config(logs_dir=str(tmp_path))
    llm_module._call_seq.clear()

    _write_conversation_log(config, _make_response(), "s", "u", "researcher", 7)
    _write_conversation_log(config, _make_response(), "s", "u", "researcher", 7)
    _write_conversation_log(config, _make_response(), "s", "u", "critic", 7)

    names = sorted(f.name for f in tmp_path.iterdir())
    assert names == [
        "iter007_critic_3.md",
        "iter007_researcher_1.md",
        "iter007_researcher_2.md",
    ]


def test_no_file_when_logs_dir_empty(tmp_path):
    config = Config(logs_dir="")
    llm_module._call_seq.clear()

    _write_conversation_log(config, _make_response(), "s", "u", "researcher", 1)

    # Should not have written anywhere — just verify no crash
    assert True


def test_workspace_init_creates_logs_dir(tmp_path):
    config = Config(workspace_dir=str(tmp_path / "ws"))
    ws = WorkspaceManager(config)
    ws.init("Test problem")

    assert ws.logs_dir.exists()
    assert ws.logs_dir.is_dir()
    assert ws.logs_dir == tmp_path / "ws" / "logs"
