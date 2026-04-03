"""Rendering subsystem: Markdown snapshots and per-agent context builders."""

from .snapshots import (
    render_background_survey,
    render_research_state_md,
    render_evidence_log_md,
    render_critique_log_md,
    render_task_md,
)
from .contexts import (
    _render_sanity_checks,
    render_background_survey_xml,
    _problem_guidelines,
    render_research_context_xml,
    render_orchestrator_research_state,
    render_orchestrator_slim_state,
    render_orchestrator_critique_log,
    render_critic_context,
    render_critic_previous_critiques,
    render_formatter_context,
    render_planner_revise_context,
)

__all__ = [
    "render_background_survey",
    "render_research_state_md",
    "render_evidence_log_md",
    "render_critique_log_md",
    "render_task_md",
    "render_background_survey_xml",
    "_problem_guidelines",
    "render_research_context_xml",
    "render_orchestrator_research_state",
    "render_orchestrator_slim_state",
    "render_orchestrator_critique_log",
    "render_critic_context",
    "render_critic_previous_critiques",
    "render_formatter_context",
    "render_planner_revise_context",
    "_render_sanity_checks",
]
