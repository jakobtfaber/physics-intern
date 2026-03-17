"""Compute-verify agent: numerical verification via code execution."""

from __future__ import annotations

from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent


class ComputeVerifyAgent(ComputationalistAgent):
    name = "compute_verify"
    prompt_file = "compute_verify.md"
    tools = ToolExecutor.VERIFY_TOOLS
