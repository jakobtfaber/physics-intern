"""Shared helpers for OpenAI-compatible providers (HuggingFace, vLLM).

Internal module — not part of the public provider API.

These helpers factor out byte-identical logic from ``huggingface.py`` and
``vllm.py``. The streaming accumulator is intentionally NOT shared — the two
providers have subtle divergences (reasoning-delta dispatch, JSON-failure
fallback) that make merging risky; see ``cleaning_plan.md`` Slice 1B.
"""

from types import SimpleNamespace


def strip_tool_messages(messages: list[dict]) -> list[dict]:
    """Remove tool-call artifacts from messages for text-only calls.

    OSS models served via OpenAI-compatible endpoints may hallucinate tool
    calls when the conversation history contains tool-call messages, even
    when no tools are offered on the current turn.  Stripping these prevents
    ``output_parse_failed`` and "Tool choice is none, but model called a
    tool" errors.
    """
    cleaned = []
    for msg in messages:
        if msg.get("role") == "tool":
            continue
        if "tool_calls" in msg:
            msg = {k: v for k, v in msg.items() if k != "tool_calls"}
            if not msg.get("content"):
                msg["content"] = "[prior tool interaction omitted]"
        cleaned.append(msg)
    return cleaned


def build_raw_tool_call(tc_id: str, name: str, arguments: str) -> SimpleNamespace:
    """Build one raw_content tool-call entry with the OpenAI SDK shape.

    ``format_assistant_message`` expects ``raw_content.tool_calls`` to be
    iterable of objects with ``.id`` and ``.function.name`` / ``.function.arguments``.
    """
    return SimpleNamespace(
        id=tc_id,
        function=SimpleNamespace(name=name, arguments=arguments),
    )
