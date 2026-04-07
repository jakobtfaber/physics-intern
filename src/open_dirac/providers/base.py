"""Base provider abstraction for LLM providers."""

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderResponse:
    """Normalized response from any LLM provider.

    Token invariant: output_tokens = reasoning_tokens + answer_tokens
    """
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str  # Normalized: "end_turn" | "max_tokens" | "tool_use"
    reasoning_tokens: int = 0
    answer_tokens: int = 0
    tool_calls: list[dict] | None = None  # [{id, name, input}] or None
    raw_content: object = None  # Provider-native content for message history
    reasoning_content: str = ""  # Reasoning trace when available


def estimate_reasoning_tokens(content: str) -> int:
    """Estimate reasoning tokens from <think>...</think> blocks in content.

    Handles standard <think>...</think> format and Qwen3 format (missing opening tag).
    Returns estimated token count (word_count * 1.3) or 0.
    """
    if not content:
        return 0

    # Try standard <think>...</think> format first
    match = re.search(r'<think>(.*?)</think>', content, re.DOTALL)
    if match:
        thinking_text = match.group(1)
    elif "</think>" in content:
        # Qwen3 Thinking format: may only have </think> without opening tag
        # Everything before </think> is reasoning content
        thinking_text = content.split("</think>", 1)[0]
    else:
        return 0

    if not thinking_text.strip():
        return 0

    word_count = len(thinking_text.split())
    return int(word_count * 1.3)


def estimate_answer_tokens(text: str, tool_calls: list[dict] | None = None) -> int:
    """Estimate answer tokens from visible text + tool call arguments.

    Uses word_count * 1.3 heuristic on the visible content (text and serialized
    tool call arguments).  tool_calls uses normalized format: [{id, name, input}].
    """
    content_words = len(text.split()) if text else 0
    if tool_calls:
        for tc in tool_calls:
            args_str = json.dumps(tc.get("input", {}))
            content_words += len(args_str.split())
    return int(content_words * 1.3)


def strip_think_tags(text: str) -> str:
    """Remove <think>...</think> blocks, return visible answer text only.

    Handles:
    - Standard <think>...</think> tags
    - Bare </think> without opening tag (Qwen3, Nemotron — chat template
      inserts the opening <think> so the model only emits </think>)
    """
    if not text:
        return ""
    # Standard format: remove all <think>...</think> blocks
    stripped = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL)
    if stripped != text:
        return stripped.strip()
    # Bare </think>: everything before it is reasoning
    if "</think>" in text:
        return text.split("</think>", 1)[1].strip()
    return text


class LLMProvider(ABC):
    """Abstract base class for LLM providers."""

    @abstractmethod
    def call(self, model: str, max_tokens: int, system: str,
             messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        """Make one API call, return normalized response.

        Tools are in OpenAI canonical format:
            [{"type": "function", "function": {"name": ..., "description": ..., "parameters": ...}}]
        """
        ...

    @abstractmethod
    def format_assistant_message(self, raw_content: object) -> dict:
        """Wrap raw_content into a message dict for conversation history."""
        ...

    @abstractmethod
    def build_tool_result_messages(self, tool_results: list[dict]) -> list[dict]:
        """Return message(s) for tool results.

        tool_results: [{"tool_call_id": ..., "name": ..., "output": ..., "is_error": ...}]
        """
        ...

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        """Pre-process messages before sending to provider.

        Default: return messages unchanged. Providers can override to strip
        thinking blocks or perform other context-saving transformations.
        """
        return messages
