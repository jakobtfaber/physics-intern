"""OpenAI provider adapter."""

import os

from .base import LLMProvider, ProviderResponse

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


class OpenAIProvider(LLMProvider):
    """OpenAI API provider."""

    def __init__(self, api_key: str = "", reasoning_effort: str = "",
                 timeout: float = 600.0, **kwargs):
        try:
            from openai import OpenAI
        except ImportError:
            raise ImportError(
                "openai package required. Install with: uv sync --extra openai"
            )
        self._client = OpenAI(
            api_key=api_key or os.environ.get("OPENAI_API_KEY", ""),
            timeout=timeout,
        )
        self._reasoning_effort = reasoning_effort

    def call(self, model: str, max_tokens: int, system: str,
             messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        # OpenAI uses system as first message
        oai_messages = [{"role": "system", "content": system}] + messages

        kwargs = dict(
            model=model,
            max_completion_tokens=max_tokens,
            messages=oai_messages,
        )
        if tools:
            kwargs["tools"] = tools  # Already in OpenAI canonical format
        elif self._reasoning_effort:
            # reasoning_effort is incompatible with tools on /v1/chat/completions
            kwargs["reasoning_effort"] = self._reasoning_effort

        response = self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        text = choice.message.content or ""

        # Extract tool calls
        tool_calls = None
        if choice.message.tool_calls:
            import json
            tool_calls = []
            for tc in choice.message.tool_calls:
                args_str = tc.function.arguments or ""
                try:
                    parsed_args = json.loads(args_str) if args_str else {}
                except (json.JSONDecodeError, ValueError):
                    if tc.function.name == "execute_python":
                        parsed_args = {"code": args_str}
                    else:
                        parsed_args = {"raw": args_str}
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": parsed_args,
                })

        stop_reason = _STOP_REASON_MAP.get(choice.finish_reason, choice.finish_reason)

        # Extract native reasoning token breakdown when available
        reasoning_tokens = 0
        if hasattr(response.usage, 'completion_tokens_details') and response.usage.completion_tokens_details:
            reasoning_tokens = getattr(response.usage.completion_tokens_details, 'reasoning_tokens', 0) or 0
        answer_tokens = max(0, response.usage.completion_tokens - reasoning_tokens)

        return ProviderResponse(
            text=text,
            input_tokens=response.usage.prompt_tokens,
            output_tokens=response.usage.completion_tokens,
            stop_reason=stop_reason,
            reasoning_tokens=reasoning_tokens,
            answer_tokens=answer_tokens,
            tool_calls=tool_calls,
            raw_content=choice.message,
        )

    def format_assistant_message(self, raw_content: object) -> dict:
        # raw_content is the ChatCompletionMessage object
        msg = {"role": "assistant", "content": raw_content.content}
        if raw_content.tool_calls:
            msg["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in raw_content.tool_calls
            ]
        return msg

    def build_tool_result_messages(self, tool_results: list[dict]) -> list[dict]:
        """OpenAI: separate role='tool' message per result."""
        messages = []
        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_call_id"],
                "content": tr["output"],
            })
        return messages
