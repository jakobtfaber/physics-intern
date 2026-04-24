# OpenDirac Cleaning Plan

A staged refactor of `src/open_dirac/` (~10.2k LOC). Slices are ordered from least to most coupled; earlier slices should land before later ones so the coupled core rests on a stable base.

Each slice is meant to be a self-contained pass: understand it, clean it, land it, move on. No slice should require touching code from a later slice.

---

## Slice 1 — Provider layer

DONE

---

## Slice 2 — Verification / evaluation

DONE

---

## Slice 3 — Rendering / snapshots

DONE

---

## Slice 4 — `ResearchState`

DONE

---

## Slice 5 — Baselines consolidation (`one_shot` + `rsa`)

DONE

---

## Slice 6 — Autophysicist

DONE

---

## Slice 7 — Engine + agents + validation (the coupled core)

DONE (landed as six sub-slices 7.1–7.6; `engine.py` 1,862 → ~1,080 LOC).

---

## Follow-ups (out of scope for current cleaning)

- **Resume fidelity.** `resume.reconstruct_loop_state` rebuilds a minimal
  `LoopState` from the persisted `ResearchState` alone, so consumed-once
  banners (`pending_explore_results`, `pending_compute_verdicts`,
  `pending_violations`, `pending_verified_results`, `pending_system_events`,
  `agent_failures`, `consecutive_termination_blocks`) are empty after a
  resume. Consider persisting the full `LoopState` to the workspace so
  resume preserves inter-iteration signals.

---

## Guiding principles

- **One slice = one PR.** No piggy-backing.
- **Tests stay green after each slice.** Expand coverage where a slice exposes gaps, but don't gate the slice on new test infrastructure.
- **Respect the invariants in `CLAUDE.md`** — `ResearchState` as single source of truth, MD files write-only, fresh context per call, tool schemas in OpenAI canonical format, ER immutability, scaffolding-owned iteration counter.
- **No behaviour changes unless explicitly flagged.** Refactors should be mechanically verifiable; behavioural fixes get their own commits.
- **Defer the engine.** It's the biggest file and the biggest temptation. It comes last for a reason.
