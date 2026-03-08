"""Compressor agent: reduces file sizes while preserving essential info."""

from ..llm import LLMResponse
from .base import BaseAgent


class CompressorAgent(BaseAgent):
    name = "compressor"
    prompt_file = "compressor.md"

    def build_context(self, task: dict, iteration: int) -> str:
        target_file = task.get("target_file", "")
        content = self.workspace.read_file(target_file)
        return f"# File to compress: {target_file}\n\n{content}"

    def process_response(self, response: LLMResponse, task: dict, iteration: int):
        """Archive original, write compressed version."""
        target_file = task.get("target_file", "")
        self.workspace.archive_file(target_file)
        self.workspace.write_file(target_file, response.text)
