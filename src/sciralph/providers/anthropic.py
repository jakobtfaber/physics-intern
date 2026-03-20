"""Anthropic provider adapter."""

import anthropic

from .base import LLMProvider, ProviderResponse

# Effort levels in descending order, used by _call_with_thinking_recovery
# to step down when thinking exhausts the token budget.
_EFFORT_FALLBACK = {"max": "high", "high": "medium", "medium": "low"}


class AnthropicProvider(LLMProvider):
    """Anthropic Claude API provider."""

    def __init__(self, api_key: str = "", reasoning_budget: int = 0,
                 thinking: bool = False, effort: str = "",
                 timeout: float = 600.0, **kwargs):
        self._client = anthropic.Anthropic(api_key=api_key)
        self._thinking = thinking or reasoning_budget > 0  # backward compat
        self._reasoning_budget = reasoning_budget
        self._effort = effort
        self._timeout = timeout

    def call(self, model: str, max_tokens: int, system: str,
             messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        if self._thinking:
            return self._call_with_thinking_recovery(
                model, max_tokens, system, messages, tools,
                effort=self._effort,
            )

        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
        )
        if tools:
            kwargs["tools"] = self._transform_tools(tools)
        return self._stream_call(kwargs)

    def _call_with_thinking_recovery(
        self, model: str, max_tokens: int, system: str,
        messages: list[dict], tools: list[dict] | None,
        effort: str,
    ) -> ProviderResponse:
        """Call with adaptive thinking; retry at lower effort on token exhaustion.

        When the model spends all tokens on thinking (stop_reason=max_tokens,
        no visible text, no tool calls), step effort down and retry once.
        """
        thinking_cfg = {"type": "adaptive"}
        kwargs = dict(
            model=model,
            max_tokens=max_tokens,
            system=system,
            messages=messages,
            thinking=thinking_cfg,
            temperature=1.0,  # Required by Anthropic when thinking is enabled
        )
        if effort:
            kwargs["output_config"] = {"effort": effort}
        if tools:
            kwargs["tools"] = self._transform_tools(tools)

        resp = self._stream_call(kwargs)

        # Detect thinking exhaustion: all tokens went to thinking,
        # nothing left for the answer or tool calls.
        if (resp.stop_reason == "max_tokens"
                and not resp.text.strip()
                and not resp.tool_calls):
            fallback_effort = _EFFORT_FALLBACK.get(effort or "high")
            if fallback_effort:
                kwargs["output_config"] = {"effort": fallback_effort}
                resp = self._stream_call(kwargs)

        return resp

    def _stream_call(self, kwargs: dict) -> ProviderResponse:
        """Execute a streaming API call and build a ProviderResponse."""
        with self._client.messages.stream(
            **kwargs,
            timeout=self._timeout,
        ) as stream:
            response = stream.get_final_message()

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

        # Estimate reasoning vs answer token split.
        # Anthropic output_tokens includes thinking; estimate answer from visible text.
        output_tokens = response.usage.output_tokens
        content_words = len(text.split()) if text else 0
        answer_tokens = int(content_words * 1.3)
        reasoning_tokens = max(0, output_tokens - answer_tokens)

        return ProviderResponse(
            text=text,
            input_tokens=response.usage.input_tokens,
            output_tokens=output_tokens,
            stop_reason=response.stop_reason,
            reasoning_tokens=reasoning_tokens,
            answer_tokens=answer_tokens,
            tool_calls=tool_calls,
            raw_content=response.content,
        )

    def format_assistant_message(self, raw_content: object) -> dict:
        return {"role": "assistant", "content": raw_content}

    def prepare_messages(self, messages: list[dict]) -> list[dict]:
        """Strip thinking blocks from older assistant turns to control context growth.

        The Anthropic API requires thinking blocks from the most recent assistant
        turn to be preserved, but older turns can have thinking stripped. This
        prevents O(rounds^2) context growth from accumulated thinking tokens.
        """
        if not self._thinking:
            return messages

        # Find the last assistant message index
        last_asst_idx = -1
        for i in range(len(messages) - 1, -1, -1):
            if messages[i].get("role") == "assistant":
                last_asst_idx = i
                break
        if last_asst_idx < 0:
            return messages

        result = []
        for i, msg in enumerate(messages):
            if msg.get("role") == "assistant" and i < last_asst_idx:
                content = msg.get("content")
                if content and isinstance(content, list):
                    filtered = [
                        block for block in content
                        if getattr(block, "type", None) not in ("thinking", "redacted_thinking")
                    ]
                    if filtered:
                        result.append({"role": "assistant", "content": filtered})
                    else:
                        # All blocks were thinking — keep original to avoid empty content
                        result.append(msg)
                else:
                    result.append(msg)
            else:
                result.append(msg)
        return result

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
