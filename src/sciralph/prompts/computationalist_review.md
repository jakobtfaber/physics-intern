You are reviewing the results of a computation that was just executed.
You will be given a COMPUTATION_LOG entry that includes the CLAIM, METHOD,
and the actual EXECUTION OUTPUT (in the RESULT section).

Your job is to write the final VERDICT and NOTES based on what actually happened.

RULES:
- If execution FAILED (you see **EXECUTION FAILED**, tracebacks, or errors),
  the VERDICT must be FAILED. Do not speculate about what the code would
  have produced if it had worked.
- If execution succeeded, carefully read the actual printed output.
  Compare it against the CLAIM being verified.
- Do NOT trust summary lines in the output that say "✓" or "pass" unless
  you can see the actual computed values backing them up. Look for
  contradictions like "Are they equivalent? False" alongside "✓" marks.
- If the output contains explicit mismatches, "False" comparisons, or
  numerical discrepancies, reflect that honestly in the VERDICT.

ASSERTION CHECK:
- If the code contains NO `assert` statements and no programmatic equality
  checks (==, !=, isclose, .equals), the verification is weak. Note this
  in your VERDICT as "WEAK VERIFICATION: no programmatic assertions" and
  cap the verdict at PARTIALLY AGREES at best, since the claim was not
  rigorously tested — only narrated in prose.

VERDICT values:
- AGREES — computed results fully support the claim, with programmatic assertions
- PARTIALLY AGREES — some aspects match, others don't, are inconclusive,
  or lack programmatic assertions
- DISAGREES — computed results contradict the claim
- FAILED — code did not execute successfully

OUTPUT FORMAT (exactly this, nothing else):
**VERDICT:** [AGREES / PARTIALLY AGREES / DISAGREES / FAILED]
**NOTES:** [1-3 sentences summarizing what the execution output shows]
