"""Compute-explore agent: exploratory computation via code execution."""

from __future__ import annotations

from ..tools import ToolExecutor
from .computationalist import ComputationalistAgent


class ComputeExploreAgent(ComputationalistAgent):
    name = "compute_explore"
    prompt_file = "compute_explore.md"
    tools = ToolExecutor.EXPLORE_TOOLS
