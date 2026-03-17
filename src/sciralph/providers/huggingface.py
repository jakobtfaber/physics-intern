"""HuggingFace Inference Providers adapter."""

import json
import os
import re
from types import SimpleNamespace

from rich.console import Console

from .base import LLMProvider, ProviderResponse, estimate_reasoning_tokens

_STOP_REASON_MAP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
}


class HuggingFaceProvider(LLMProvider):
    """HuggingFace Inference Providers via native InferenceClient."""

    @staticmethod
    def _strip_tool_messages(messages: list[dict]) -> list[dict]:
        """Remove tool-call artifacts from messages for text-only calls.

        OSS models served via HF Inference Providers may generate tool calls
        even when tools are not provided, if the conversation history contains
        tool-call messages.  Stripping these prevents output_parse_failed and
        'Tool choice is none, but model called a tool' errors.
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

    def __init__(self, api_key: str = "", hf_provider: str | None = None,
                 timeout: float | None = None, reasoning_format: str = "",
                 **kwargs):
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
        if timeout is not None:
            client_kwargs["timeout"] = timeout
        self._client = InferenceClient(**client_kwargs)
        self._reasoning_format = reasoning_format

    @staticmethod
    def _extract_failed_generation(exc: Exception) -> str:
        """Extract the failed_generation string from an HF error.

        Tries multiple strategies because the HF exception format varies:
        1. exc.response.json() → structured body with failed_generation key
        2. exc.response.text → raw JSON body parsed manually
        3. str(exc) → brace-counting extraction as last resort
        """
        # Strategy 1: structured response body via response.json()
        try:
            body = exc.response.json()  # type: ignore[union-attr]
            fg = body.get("failed_generation") or ""
            if not fg:
                err = body.get("error", {})
                if isinstance(err, dict):
                    fg = err.get("failed_generation") or ""
            if fg:
                return fg
        except Exception:
            pass

        # Strategy 2: parse raw response text
        try:
            raw_text = exc.response.text  # type: ignore[union-attr]
            body = json.loads(raw_text)
            fg = body.get("failed_generation") or ""
            if not fg:
                err = body.get("error", {})
                if isinstance(err, dict):
                    fg = err.get("failed_generation") or ""
            if fg:
                return fg
        except Exception:
            pass

        # Strategy 3: extract from str(exc) via brace-counting
        # HF includes the response body in the exception string.
        # Find the {"name": ...} tool-call object after "failed_generation".
        exc_str = str(exc)
        fg_idx = exc_str.find("failed_generation")
        if fg_idx < 0:
            return ""
        search_from = exc_str[fg_idx:]
        obj_start = search_from.find('{"name"')
        if obj_start < 0:
            return ""
        obj = search_from[obj_start:]
        # Count braces to find matching closing }
        depth = 0
        for i, ch in enumerate(obj):
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    return obj[: i + 1]
        # No matching brace — return everything (repair will strip trailing })
        return obj

    def _repair_failed_tool_call(self, exc: Exception) -> ProviderResponse | None:
        """Attempt to salvage a tool call from a tool_use_failed error.

        OSS models sometimes emit raw code instead of valid JSON for tool-call
        arguments, e.g. {"name": "execute_python", "arguments": import numpy ...}.
        The HF backend rejects this, but includes the raw generation in the error
        body.  We parse it out and build a synthetic ProviderResponse so the
        agentic loop can proceed instead of burning 10 identical retries.

        Returns None if repair is not possible.
        """
        console = Console(stderr=True)
        failed = self._extract_failed_generation(exc)
        if not failed:
            console.print(
                "[dim yellow]⚕ Repair: no failed_generation found[/]",
                highlight=False,
            )
            return None

        # Parse tool name
        name_match = re.search(r'"name"\s*:\s*"([^"]+)"', failed)
        if not name_match:
            return None
        tool_name = name_match.group(1)

        # Extract raw arguments after "arguments":
        arg_match = re.search(r'"arguments"\s*:\s*', failed)
        if not arg_match:
            return None
        raw_args = failed[arg_match.end():]
        # Strip a single trailing } that closes the outer tool-call object
        raw_args = raw_args.rstrip()
        if raw_args.endswith("}"):
            raw_args = raw_args[:-1].rstrip()

        # Try parsing as valid JSON first (backend rejected it for other reasons)
        try:
            parsed_args = json.loads(raw_args)
            if isinstance(parsed_args, str):
                # Quoted string — unwrap and wrap properly for execute_python
                parsed_args = {"code": parsed_args}
        except (json.JSONDecodeError, ValueError):
            # Raw code — wrap it for execute_python
            if tool_name == "execute_python":
                parsed_args = {"code": raw_args}
            else:
                return None

        console.print(
            f"[bold yellow]⚕ Repaired malformed tool call:[/] "
            f"{tool_name}({len(str(parsed_args))} chars)",
            highlight=False,
        )

        # Build synthetic raw_content compatible with format_assistant_message
        fake_tc = SimpleNamespace(
            id="repaired_0",
            function=SimpleNamespace(
                name=tool_name,
                arguments=json.dumps(parsed_args),
            ),
        )
        raw_content = SimpleNamespace(
            content="",
            tool_calls=[fake_tc],
        )

        return ProviderResponse(
            text="",
            input_tokens=0,
            output_tokens=0,
            stop_reason="tool_use",
            tool_calls=[{
                "id": "repaired_0",
                "name": tool_name,
                "input": parsed_args,
            }],
            raw_content=raw_content,
        )

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
        else:
            # Strip tool-call history so OSS models don't hallucinate tool calls
            kwargs["messages"] = self._strip_tool_messages(hf_messages)

        try:
            response = self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            exc_msg = str(exc).lower()
            if "tool_use_failed" in exc_msg or "post processor" in exc_msg:
                repaired = self._repair_failed_tool_call(exc)
                if repaired is not None:
                    return repaired
            raise

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

        # Reasoning token breakdown based on model's reasoning format
        reasoning_tokens = 0
        answer_tokens = output_tokens
        if self._reasoning_format == "separate_field":
            # Models like Kimi, GPT-OSS: completion_tokens includes reasoning.
            # Count words in visible text AND tool call arguments so tool-use
            # rounds don't misattribute all output_tokens to reasoning.
            content_words = len(text.split()) if text else 0
            if choice.message.tool_calls:
                for tc in choice.message.tool_calls:
                    args_str = tc.function.arguments if isinstance(tc.function.arguments, str) else ""
                    content_words += len(args_str.split())
            answer_tokens = int(content_words * 1.3)
            reasoning_tokens = max(0, output_tokens - answer_tokens)
        elif self._reasoning_format == "think_tags":
            # Models like DeepSeek: reasoning in <think>...</think> tags
            reasoning_tokens = estimate_reasoning_tokens(text)
            answer_tokens = max(0, output_tokens - reasoning_tokens)

        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            reasoning_tokens=reasoning_tokens,
            answer_tokens=answer_tokens,
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
