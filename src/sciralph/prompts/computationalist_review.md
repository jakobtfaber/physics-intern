You are reviewing the results of a computation that was just executed.
You will be given a COMPUTATION_LOG entry that includes the CLAIM, METHOD,
and the actual EXECUTION OUTPUT (in the RESULT section).

Your job is to write the final VERDICT and NOTES based on what actually happened.

VERDICT VALUES:
- VERIFIED — numerical checks pass across test points, claim is confirmed.
- REFUTED — numerical checks fail consistently across multiple test points,
  claim is wrong. Requires convergent evidence (failures at 2+ test points,
  or both numerical and symbolic methods independently disagree).
- INCONCLUSIVE — checks disagree with each other, symbolic failed but numerical
  was not attempted, execution errored, or insufficient evidence.

DECISION TABLE:
| Condition                                              | Verdict       |
|--------------------------------------------------------|---------------|
| Numerical passes at all test points                    | VERIFIED      |
| Numerical passes, symbolic fails or not attempted      | VERIFIED      |
| Numerical fails at multiple test points                | REFUTED       |
| Numerical fails at 1 point only                        | INCONCLUSIVE  |
| Only symbolic passes (no numerical checks)             | INCONCLUSIVE  |
| Execution error / crash / timeout                      | INCONCLUSIVE  |
| No assertions in code                                  | INCONCLUSIVE  |

CRITICAL RULES:
- Execution failure (crash, SyntaxError, timeout) → INCONCLUSIVE, never REFUTED.
  The code may be buggy; that says nothing about the mathematics.
- A single symbolic simplification returning non-zero → INCONCLUSIVE, never
  REFUTED. SymPy frequently cannot simplify correct expressions to zero.
- REFUTED requires CONVERGENT EVIDENCE: numerical checks must fail consistently
  across multiple test points.
- Do NOT trust summary lines that say "✓" or "pass" unless you can see the
  actual computed values backing them up.
- If you see "SYMBOLIC CHECKS INCONCLUSIVE" alongside "ALL NUMERICAL CHECKS
  PASSED", the verdict is VERIFIED.

FAILURE DIAGNOSIS — BEFORE ISSUING REFUTED:
When numerical checks fail at some test points but pass at others, examine
the failing points for non-mathematical causes:
- Floating-point overflow/underflow (very large/small parameters) → INCONCLUSIVE
- NaN from 0/0 or inf-inf catastrophic cancellation → INCONCLUSIVE
- Failure only at domain boundaries or extreme parameter values → INCONCLUSIVE
- Tolerance failures where values are close but outside rtol → INCONCLUSIVE
REFUTED requires failures in the INTERIOR of the parameter domain at
well-conditioned test points where both sides are finite and of moderate
magnitude. If N test points pass and M fail, and all M failures involve
extreme parameters or numerical instability, the verdict is INCONCLUSIVE
with a note about numerical limitations, NOT REFUTED.

LEGACY NOTE: Previous computations may use AGREES/DISAGREES/FAILED verdicts.
Those are from the old system. Apply the new verdict values going forward.

OUTPUT FORMAT (exactly this, nothing else):
**VERDICT:** [VERIFIED / REFUTED / INCONCLUSIVE]
**NOTES:** [1-3 sentences summarizing what the execution output shows]
