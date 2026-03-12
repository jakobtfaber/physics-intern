"""Google Gemini provider adapter."""

import os

from .base import LLMProvider, ProviderResponse


class GoogleProvider(LLMProvider):
    """Google Gemini API provider via google-genai."""

    def __init__(self, api_key: str = "", **kwargs):
        try:
            from google import genai
        except ImportError:
            raise ImportError(
                "google-genai package required. Install with: uv sync --extra google"
            )
        self._genai = genai
        self._client = genai.Client(
            api_key=api_key or os.environ.get("GOOGLE_API_KEY", "")
        )

    def call(self, model: str, max_tokens: int, system: str,
             messages: list[dict], tools: list[dict] | None = None) -> ProviderResponse:
        genai = self._genai

        # Build contents from messages
        contents = []
        for msg in messages:
            role = "user" if msg["role"] == "user" else "model"
            content = msg["content"]
            if isinstance(content, str):
                contents.append(genai.types.Content(
                    role=role,
                    parts=[genai.types.Part(text=content)],
                ))
            elif isinstance(content, list):
                parts = []
                for item in content:
                    if isinstance(item, dict):
                        if item.get("type") == "text":
                            parts.append(genai.types.Part(text=item["text"]))
                        elif item.get("type") == "function_response":
                            parts.append(genai.types.Part(
                                function_response=genai.types.FunctionResponse(
                                    name=item["name"],
                                    response=item["response"],
                                )
                            ))
                    else:
                        # Pass through genai.types.Part objects
                        parts.append(item)
                if parts:
                    contents.append(genai.types.Content(role=role, parts=parts))

        # Build tool declarations
        gemini_tools = None
        if tools:
            declarations = []
            for tool in tools:
                func = tool["function"]
                # Convert JSON Schema parameters to Gemini format
                declarations.append(genai.types.FunctionDeclaration(
                    name=func["name"],
                    description=func["description"],
                    parameters=func["parameters"],
                ))
            gemini_tools = [genai.types.Tool(function_declarations=declarations)]

        config = genai.types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            tools=gemini_tools,
        )

        response = self._client.models.generate_content(
            model=model,
            contents=contents,
            config=config,
        )

        # Extract text and tool calls
        text_parts = []
        tool_calls = None

        if response.candidates and response.candidates[0].content:
            for part in response.candidates[0].content.parts:
                if part.text:
                    text_parts.append(part.text)
                elif part.function_call:
                    if tool_calls is None:
                        tool_calls = []
                    fc = part.function_call
                    tool_calls.append({
                        "id": fc.name,  # Gemini uses name as ID
                        "name": fc.name,
                        "input": dict(fc.args) if fc.args else {},
                    })

        text = "\n".join(text_parts)

        # Determine stop reason
        stop_reason = "end_turn"
        if tool_calls:
            stop_reason = "tool_use"
        elif (response.candidates
              and response.candidates[0].finish_reason
              and response.candidates[0].finish_reason.name == "MAX_TOKENS"):
            stop_reason = "max_tokens"

        # Token usage
        input_tokens = 0
        output_tokens = 0
        if response.usage_metadata:
            input_tokens = response.usage_metadata.prompt_token_count or 0
            output_tokens = response.usage_metadata.candidates_token_count or 0

        return ProviderResponse(
            text=text,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            stop_reason=stop_reason,
            tool_calls=tool_calls,
            raw_content=response.candidates[0].content if response.candidates else None,
        )

    def format_assistant_message(self, raw_content: object) -> dict:
        # raw_content is a genai.types.Content object; store as-is for re-use
        return {"role": "model", "content": raw_content}

    def build_tool_result_messages(self, tool_results: list[dict]) -> list[dict]:
        """Google: single user message with function_response parts."""
        genai = self._genai
        parts = []
        for tr in tool_results:
            parts.append(genai.types.Part(
                function_response=genai.types.FunctionResponse(
                    name=tr["name"],
                    response={"result": tr["output"], "is_error": tr["is_error"]},
                )
            ))
        return [{"role": "user", "content": parts}]
