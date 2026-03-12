"""Base provider abstraction for LLM providers."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


@dataclass
class ProviderResponse:
    """Normalized response from any LLM provider."""
    text: str
    input_tokens: int
    output_tokens: int
    stop_reason: str  # Normalized: "end_turn" | "max_tokens" | "tool_use"
    tool_calls: list[dict] | None = None  # [{id, name, input}] or None
    raw_content: object = None  # Provider-native content for message history


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
