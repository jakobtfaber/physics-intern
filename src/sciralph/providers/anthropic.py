"""Anthropic provider adapter."""

import anthropic

from .base import LLMProvider, ProviderResponse


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str = "", **kwargs):
        self._client = anthropic.Anthropic(api_key=api_key)

    def call(self, model: str, max_tokens: int, system: str,
             messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = self._transform_tools(tools)

        response = self._client.messages.create(**kwargs)

        # Extract text
        text_parts = []
        for block in response.content:
            if hasattr(block, "text"):
                text_parts.append(block.text)
        text = "\n".join(text_parts)

        # Extract tool calls
        tool_calls = None
        if response.stop_reason == "tool_use":
            tool_calls = []
            for block in response.content:
                if block.type == "tool_use":
                    tool_calls.append({
                        "id": block.id,
                        "name": block.name,
                        "input": block.input,
                    })

        return ProviderResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            stop_reason=response.stop_reason,
            tool_calls=tool_calls,
            raw_content=response.content,
        )

    def format_assistant_message(self, raw_content: object) -> dict:
        return {"role": "assistant", "content": raw_content}

    def build_tool_result_messages(self, tool_results: list[dict]) -> list[dict]:
        """Anthropic: single user message with tool_result content blocks."""
        content = []
        for tr in tool_results:
            content.append({
                "type": "tool_result",
                "tool_use_id": tr["tool_call_id"],
                "content": tr["output"],
                "is_error": tr["is_error"],
            })
        return [{"role": "user", "content": content}]

    @staticmethod
    def _transform_tools(tools: list[dict]) -> list[dict]:
        """Convert OpenAI canonical format to Anthropic format."""
        result = []
        for tool in tools:
            func = tool["function"]
            result.append({
                "name": func["name"],
                "description": func["description"],
                "input_schema": func["parameters"],
            })
        return result
