"""Answer evaluation for one-shot LLM responses.

Compares an LLM's answer against the known answer from the problem YAML.
Supports symbolic (SymPy) and numerical (float) answer types.

Symbolic comparison uses a cascading chain:
  1. simplify(candidate - truth) == 0
  2. simplify(expand(candidate - truth)) == 0
  3. simplify(candidate / truth) == 1  (ratio test)
  4. candidate.equals(truth)  (random numerical substitution)
  5. Numerical fallback for constant expressions
"""
from __future__ import annotations

import re
import traceback
from typing import Any


# ---------------------------------------------------------------------------
# Code extraction
# ---------------------------------------------------------------------------

def extract_answer_code(response_text: str) -> str | None:
    """Return the last ``python code block containing ``def answer`` in *response_text*."""
    pattern = r"```python\s*\n(.*?)```"
    blocks = re.findall(pattern, response_text, re.DOTALL)
    for block in reversed(blocks):
        if "def answer" in block:
            return block.strip()
    return None


# ---------------------------------------------------------------------------
# Answer type classification
# ---------------------------------------------------------------------------

def _classify_answer_type(answer_value: Any) -> str:
    """Return ``"numerical"`` if *answer_value* is a plain number, else ``"symbolic"``."""
    if isinstance(answer_value, (int, float)):
        return "numerical"
    try:
        float(str(answer_value).strip())
        return "numerical"
    except (ValueError, TypeError):
        return "symbolic"


# ---------------------------------------------------------------------------
# Template parsing
# ---------------------------------------------------------------------------

def _parse_template_preamble(answer_template: str) -> tuple[str, list[str]]:
    """Extract preamble code and ``answer()`` parameter names from *answer_template*.

    The preamble is everything before the ``def answer(...)`` line —
    typically ``import sympy`` and symbol definitions.
    """
    lines = answer_template.strip().splitlines()
    preamble_lines: list[str] = []
    param_names: list[str] = []
    for line in lines:
        if line.strip().startswith("def answer("):
            match = re.match(r"def answer\(([^)]*)\)", line.strip())
            if match and match.group(1).strip():
                param_names = [p.strip() for p in match.group(1).split(",")]
            break
        preamble_lines.append(line)
    return "\n".join(preamble_lines), param_names


# ---------------------------------------------------------------------------
# Numerical comparison
# ---------------------------------------------------------------------------

def _compare_numerical(candidate: Any, truth: float, rtol: float = 0.01) -> dict:
    """Compare numerical answers with relative tolerance."""
    try:
        candidate_val = float(candidate)
    except (TypeError, ValueError) as exc:
        return {
            "correct": None,
            "method": "numerical_conversion_error",
            "error": f"Cannot convert candidate to float: {exc}",
            "details": "",
        }
    if truth == 0:
        correct = abs(candidate_val) < 1e-10
    else:
        correct = abs(candidate_val - truth) / abs(truth) < rtol
    return {
        "correct": correct,
        "method": "numerical",
        "error": None,
        "details": f"candidate={candidate_val}, truth={truth}, rtol={rtol}",
    }


# ---------------------------------------------------------------------------
# Symbolic comparison
# ---------------------------------------------------------------------------

def _compare_symbolic(
    candidate: Any,
    answer_str: str,
    namespace: dict[str, Any],
) -> dict:
    """Compare symbolic answers using cascading simplification methods."""
    import sympy as sp

    # Evaluate truth expression in the shared namespace
    truth_ns = dict(namespace)
    rhs = answer_str.split("=", 1)[1].strip() if "=" in answer_str else answer_str.strip()
    try:
        truth = eval(rhs, truth_ns)  # noqa: S307
    except Exception as exc:
        return {
            "correct": None,
            "method": "truth_eval_error",
            "error": f"Truth expression eval failed: {exc}",
            "details": traceback.format_exc(),
        }

    # --- Cascading comparison chain ---

    # 1. simplify(candidate - truth) == 0
    try:
        diff = sp.simplify(candidate - truth)
        if diff == 0:
            return {"correct": True, "method": "simplify", "error": None, "details": str(diff)}
    except Exception:
        pass

    # 2. simplify(expand(candidate - truth)) == 0
    try:
        diff = sp.simplify(sp.expand(candidate - truth))
        if diff == 0:
            return {"correct": True, "method": "expand_simplify", "error": None, "details": str(diff)}
    except Exception:
        pass

    # 3. Ratio test: simplify(candidate / truth) == 1
    try:
        ratio = sp.simplify(candidate / truth)
        if ratio == 1:
            return {"correct": True, "method": "ratio", "error": None, "details": str(ratio)}
    except Exception:
        pass

    # 4. .equals() — random numerical substitution (most robust)
    try:
        if candidate.equals(truth):
            return {"correct": True, "method": "equals", "error": None, "details": "candidate.equals(truth)"}
    except Exception:
        pass

    # 5. Numerical fallback for constant expressions (no free symbols)
    try:
        c_val = complex(candidate)
        t_val = complex(truth)
        if t_val != 0 and abs(c_val - t_val) / abs(t_val) < 1e-9:
            return {
                "correct": True,
                "method": "numerical_fallback",
                "error": None,
                "details": f"candidate={c_val}, truth={t_val}",
            }
    except Exception:
        pass

    return {
        "correct": False,
        "method": "all_methods_failed",
        "error": None,
        "details": f"candidate={candidate}, truth={truth}",
    }


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def evaluate_response(response_text: str, problem_def: dict) -> dict:
    """Evaluate an LLM response against the known answer from *problem_def*.

    Returns a dict with keys:
        correct  — True / False / None (if evaluation errored)
        method   — which comparison method succeeded (or error label)
        error    — error message string, or None
        details  — human-readable extra info
    """
    answer_value = problem_def.get("answer")
    answer_template = problem_def.get("answer_template", "")

    if answer_value is None or (isinstance(answer_value, str) and not answer_value.strip()):
        return {"correct": None, "method": "no_answer", "error": "No answer in problem definition", "details": ""}

    answer_type = _classify_answer_type(answer_value)
    answer_str = str(answer_value).strip()

    # Extract candidate code from LLM response
    candidate_code = extract_answer_code(response_text)
    if candidate_code is None:
        return {
            "correct": None,
            "method": "no_code",
            "error": "No python code block with def answer found in response",
            "details": "",
        }

    # Parse template preamble for shared namespace
    preamble, param_names = _parse_template_preamble(answer_template)

    # Build shared namespace from preamble
    namespace: dict[str, Any] = {}
    try:
        if preamble.strip():
            exec(preamble, namespace)  # noqa: S102
    except Exception as exc:
        return {
            "correct": None,
            "method": "preamble_error",
            "error": f"Preamble exec failed: {exc}",
            "details": traceback.format_exc(),
        }

    # Ensure sympy is available
    if "sp" not in namespace:
        try:
            import sympy as sp
            namespace["sp"] = sp
        except ImportError:
            pass

    # Execute candidate code in the shared namespace
    candidate_ns = dict(namespace)
    try:
        exec(candidate_code, candidate_ns)  # noqa: S102
    except Exception as exc:
        return {
            "correct": None,
            "method": "candidate_exec_error",
            "error": f"Candidate code exec failed: {exc}",
            "details": traceback.format_exc(),
        }

    if "answer" not in candidate_ns or not callable(candidate_ns["answer"]):
        return {
            "correct": None,
            "method": "no_answer_func",
            "error": "Candidate code has no callable answer()",
            "details": "",
        }

    # Call candidate's answer() with canonical symbols from preamble
    try:
        if param_names:
            args = [candidate_ns[name] for name in param_names]
            candidate_result = candidate_ns["answer"](*args)
        else:
            candidate_result = candidate_ns["answer"]()
    except Exception as exc:
        return {
            "correct": None,
            "method": "candidate_call_error",
            "error": f"answer() call failed: {exc}",
            "details": traceback.format_exc(),
        }

    # Compare
    if answer_type == "numerical":
        return _compare_numerical(candidate_result, float(answer_str))
    else:
        return _compare_symbolic(candidate_result, answer_str, namespace)
