"""Tests for reasoning token tracking across providers."""

from sciralph.providers.base import ProviderResponse, estimate_reasoning_tokens


# ── estimate_reasoning_tokens ──────────────────────────────────────────────

class TestEstimateReasoningTokens:
    def test_standard_think_tags(self):
        content = "Hello <think>Let me reason about this step by step</think> The answer is 4."
        tokens = estimate_reasoning_tokens(content)
        assert tokens > 0
        # 8 words * 1.3 = 10
        assert tokens == int(len("Let me reason about this step by step".split()) * 1.3)

    def test_qwen3_format_missing_opening_tag(self):
        """Qwen3 Thinking format: no <think>, just </think>."""
        content = "Let me think about this carefully</think>The answer is 42."
        tokens = estimate_reasoning_tokens(content)
        assert tokens > 0
        # Words before </think>
        assert tokens == int(len("Let me think about this carefully".split()) * 1.3)

    def test_empty_content(self):
        assert estimate_reasoning_tokens("") == 0
        assert estimate_reasoning_tokens(None) == 0

    def test_no_think_blocks(self):
        """Content without think tags returns 0."""
        assert estimate_reasoning_tokens("Just a normal response") == 0

    def test_empty_think_block(self):
        assert estimate_reasoning_tokens("<think></think>The answer") == 0

    def test_multiline_think_block(self):
        content = "<think>\nStep 1: Consider X\nStep 2: Apply Y\n</think>\nResult: Z"
        tokens = estimate_reasoning_tokens(content)
        assert tokens > 0


# ── ProviderResponse invariant ─────────────────────────────────────────────

class TestProviderResponseInvariant:
    def test_invariant_with_reasoning(self):
        """output_tokens == reasoning_tokens + answer_tokens when both set."""
        resp = ProviderResponse(
            text="answer",
            input_tokens=100,
            output_tokens=500,
            stop_reason="end_turn",
            reasoning_tokens=300,
            answer_tokens=200,
        )
        assert resp.output_tokens == resp.reasoning_tokens + resp.answer_tokens

    def test_defaults_zero(self):
        """New fields default to 0 for backward compatibility."""
        resp = ProviderResponse(
            text="answer",
            input_tokens=100,
            output_tokens=500,
            stop_reason="end_turn",
        )
        assert resp.reasoning_tokens == 0
        assert resp.answer_tokens == 0

    def test_anthropic_paradigm(self):
        """Anthropic: output_tokens includes thinking, estimate split from text."""
        text = "The Hawking temperature is T equals h-bar kappa"
        content_words = len(text.split())
        answer_tokens = int(content_words * 1.3)
        output_tokens = 500
        reasoning_tokens = max(0, output_tokens - answer_tokens)
        resp = ProviderResponse(
            text=text,
            input_tokens=100,
            output_tokens=output_tokens,
            stop_reason="end_turn",
            reasoning_tokens=reasoning_tokens,
            answer_tokens=answer_tokens,
        )
        assert resp.output_tokens >= resp.reasoning_tokens + resp.answer_tokens

    def test_openai_paradigm(self):
        """OpenAI: native reasoning_tokens from completion_tokens_details."""
        resp = ProviderResponse(
            text="4",
            input_tokens=50,
            output_tokens=200,
            stop_reason="end_turn",
            reasoning_tokens=180,
            answer_tokens=20,
        )
        assert resp.output_tokens == resp.reasoning_tokens + resp.answer_tokens

    def test_google_paradigm(self):
        """Google: answer = candidates_token_count, reasoning = thoughts_token_count."""
        answer = 100
        reasoning = 400
        resp = ProviderResponse(
            text="result",
            input_tokens=50,
            output_tokens=answer + reasoning,
            stop_reason="end_turn",
            reasoning_tokens=reasoning,
            answer_tokens=answer,
        )
        assert resp.output_tokens == resp.reasoning_tokens + resp.answer_tokens

    def test_huggingface_think_tags_paradigm(self):
        """HuggingFace with think_tags: estimate reasoning from content."""
        text = "<think>Step 1 step 2 step 3 reasoning</think>The answer is 4."
        reasoning = estimate_reasoning_tokens(text)
        output_tokens = 100
        answer = max(0, output_tokens - reasoning)
        resp = ProviderResponse(
            text=text,
            input_tokens=50,
            output_tokens=output_tokens,
            stop_reason="end_turn",
            reasoning_tokens=reasoning,
            answer_tokens=answer,
        )
        assert resp.reasoning_tokens + resp.answer_tokens == resp.output_tokens


# ── LLMResponse / AgentResult fields ──────────────────────────────────────

class TestLLMWrapperFields:
    def test_llm_response_defaults(self):
        from sciralph.llm import LLMResponse
        resp = LLMResponse(text="hi", input_tokens=10, output_tokens=5,
                           stop_reason="end_turn", duration=1.0)
        assert resp.reasoning_tokens == 0
        assert resp.answer_tokens == 0

    def test_llm_response_with_reasoning(self):
        from sciralph.llm import LLMResponse
        resp = LLMResponse(text="hi", input_tokens=10, output_tokens=50,
                           stop_reason="end_turn", duration=1.0,
                           reasoning_tokens=30, answer_tokens=20)
        assert resp.reasoning_tokens == 30
        assert resp.answer_tokens == 20

    def test_agent_result_defaults(self):
        from sciralph.llm import AgentResult
        result = AgentResult(text="done")
        assert result.total_reasoning_tokens == 0
        assert result.total_answer_tokens == 0

    def test_agent_result_with_reasoning(self):
        from sciralph.llm import AgentResult
        result = AgentResult(text="done", total_reasoning_tokens=500,
                             total_answer_tokens=200)
        assert result.total_reasoning_tokens == 500
        assert result.total_answer_tokens == 200
