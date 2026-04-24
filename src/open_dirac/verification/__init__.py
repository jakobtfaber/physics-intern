"""Verification subsystem: independent answer checking and process audit."""

from .evaluate import evaluate_response, extract_answer_code
from .workspace import (
    WorkspaceContents,
    RerunResult,
    REFERENCES_DIR,
    load_workspace,
    load_reference_file,
    rerun_computations,
)
from .formal_eval import (
    FormalEvalResult,
    run_formal_evaluation,
    render_formal_evaluation,
    write_formal_eval_report,
    load_or_run_formal_eval,
)

__all__ = [
    "evaluate_response",
    "extract_answer_code",
    "WorkspaceContents",
    "RerunResult",
    "REFERENCES_DIR",
    "load_workspace",
    "load_reference_file",
    "rerun_computations",
    "FormalEvalResult",
    "run_formal_evaluation",
    "render_formal_evaluation",
    "write_formal_eval_report",
    "load_or_run_formal_eval",
]
