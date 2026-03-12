"""HuggingFace Inference Providers adapter."""

import json
import os

from .base import LLMProvider, ProviderResponse

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


class HuggingFaceProvider(LLMProvider):
    """HuggingFace Inference Providers via native InferenceClient."""

    def __init__(self, api_key: str = "", hf_provider: str | None = None, **kwargs):
        try:
            from huggingface_hub import InferenceClient
        except ImportError:
            raise ImportError(
                "huggingface-hub package required. Install with: uv sync --extra huggingface"
            )
        token = api_key or os.environ.get("HF_TOKEN", "")
        client_kwargs = {"token": token}
        if hf_provider:
            client_kwargs["provider"] = hf_provider
        self._client = InferenceClient(**client_kwargs)

    def call(self, model: str, max_tokens: int, system: str,
             messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        # HF uses OpenAI-compatible chat completions with system as first message
        hf_messages = [{"role": "system", "content": system}] + messages

        kwargs = dict(
            model=model,
            messages=hf_messages,
            max_tokens=max_tokens,
        )
        if tools:
            kwargs["tools"] = tools  # Already in OpenAI canonical format

        response = self._client.chat.completions.create(**kwargs)

        choice = response.choices[0]
        text = choice.message.content or ""

        # Extract tool calls
        tool_calls = None
        if choice.message.tool_calls:
            tool_calls = []
            for tc in choice.message.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "input": json.loads(tc.function.arguments)
                    if isinstance(tc.function.arguments, str)
                    else tc.function.arguments,
                })

        stop_reason = _STOP_REASON_MAP.get(choice.finish_reason, choice.finish_reason)

        # Token usage
        input_tokens = 0
        output_tokens = 0
        if response.usage:
            input_tokens = response.usage.prompt_tokens or 0
            output_tokens = response.usage.completion_tokens or 0

        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            raw_content=choice.message,
        )

    def format_assistant_message(self, raw_content: object) -> dict:
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
        """HF: separate role='tool' messages (OpenAI-compatible)."""
        messages = []
        for tr in tool_results:
            messages.append({
                "role": "tool",
                "tool_call_id": tr["tool_call_id"],
                "content": tr["output"],
            })
        return messages
