# Vendored from https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Super-120B-A12B-BF16/blob/main/super_v3_reasoning_parser.py
from vllm.reasoning.abs_reasoning_parsers import ReasoningParserManager
from vllm.reasoning.deepseek_r1_reasoning_parser import DeepSeekR1ReasoningParser


@ReasoningParserManager.register_module("super_v3")
class SuperV3ReasoningParser(DeepSeekR1ReasoningParser):
    def extract_reasoning(self, model_output, request):
        reasoning_content, final_content = super().extract_reasoning(
            model_output, request
        )
        if (
            hasattr(request, "chat_template_kwargs")
            and request.chat_template_kwargs
            and (
                request.chat_template_kwargs.get("enable_thinking") is False
                or request.chat_template_kwargs.get("force_nonempty_content") is True
            )
            and final_content is None
        ):
            reasoning_content, final_content = None, reasoning_content

        return reasoning_content, final_content
