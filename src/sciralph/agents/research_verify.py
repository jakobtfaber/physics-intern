"""Research-verify agent: analytical/structural verification without code execution."""

from __future__ import annotations

from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent


class ResearchVerifyAgent(ComputationalistAgent):
    name = "research_verify"
    prompt_file = "research_verify.md"
    tools = ToolExecutor.RESEARCH_VERIFY_TOOLS
