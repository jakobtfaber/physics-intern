#!/usr/bin/env python3
"""Drive ``stress_test.py`` against multiple serve replicas in parallel.

Reads a sweep dir's ``plan.json`` (or accepts an explicit ``--jobs`` map),
fans out one stress test per replica, and produces:

- ``stress_<variant>.json`` — full per-variant payload
- ``stress_<variant>.log`` — stress_test.py stderr stream
- ``STRESS_SUMMARY.md`` — failure-rate + throughput table across variants

This is the *stability* counterpart to ``run_pp_sweep.py``: it doesn't submit
new serve jobs, it just hammers existing replicas with realistic long-context
load and reports who survived.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
STRESS_SCRIPT = PROJECT_ROOT / "scripts" / "stress_test.py"

logger = logging.getLogger("stress_sweep")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


@dataclass
class Variant:
    name: str
    job_id: str
    description: str = ""


def _load_pp_sweep(sweep_dir: Path) -> list[Variant]:
    """Reconstruct (variant_name, job_id) pairs from a pp_sweep dir."""
    plan = json.loads((sweep_dir / "plan.json").read_text())
    jobs = json.loads((sweep_dir / "jobs.json").read_text())
    variants: list[Variant] = []
    for v in plan["variants"]:
        if v["name"] not in jobs:
            continue
        variants.append(
            Variant(
                name=v["name"],
                job_id=jobs[v["name"]],
                description=(
                    f"pp={v['pp']} max_seqs={v['max_num_seqs']} "
                    f"batched={v['max_num_batched_tokens']} "
                    f"cudagraph={v['cudagraph_mode']}"
                ),
            )
        )
    return variants


async def _run_one(
    variant: Variant,
    out_dir: Path,
    concurrency: int,
    duration_s: int,
    max_tokens: int,
    stream_idle_timeout: int,
    request_cap: int,
) -> dict:
    """Spawn ``stress_test.py`` against a single variant and parse JSON out."""
    out_path = out_dir / f"stress_{variant.name}.json"
    log_path = out_dir / f"stress_{variant.name}.log"
    cmd = [
        "uv",
        "run",
        "--no-sync",
        "python",
        str(STRESS_SCRIPT),
        "--serve-job",
        variant.job_id,
        "--concurrency",
        str(concurrency),
        "--duration-s",
        str(duration_s),
        "--max-tokens",
        str(max_tokens),
        "--stream-idle-timeout",
        str(stream_idle_timeout),
        "--request-cap",
        str(request_cap),
    ]
    logger.info("[%s] running stress test (job=%s)", variant.name, variant.job_id)
    with log_path.open("w") as log_fh:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_fh,
            cwd=str(PROJECT_ROOT),
        )
        stdout, _ = await proc.communicate()
    if proc.returncode != 0:
        logger.error(
            "[%s] stress_test.py exited %d (see %s)",
            variant.name,
            proc.returncode,
            log_path,
        )
        return {
            "variant": variant.name,
            "job_id": variant.job_id,
            "description": variant.description,
            "error": f"stress_test exited {proc.returncode}",
        }
    out_path.write_bytes(stdout)
    payload = json.loads(stdout)
    return {
        "variant": variant.name,
        "job_id": variant.job_id,
        "description": variant.description,
        "summary": payload["summary"],
        "num_failures_logged": len(payload["failures"]),
    }


def _write_summary(
    results: list[dict], out_dir: Path, args: argparse.Namespace
) -> Path:
    """Render a Markdown table comparing stability + throughput per variant."""
    summary_path = out_dir / "STRESS_SUMMARY.md"
    lines: list[str] = []
    lines.append(f"# DeepSeek V4 Pro stress sweep — {out_dir.name}")
    lines.append("")
    lines.append(f"- concurrency: `{args.concurrency}`")
    lines.append(f"- duration:    `{args.duration_s}s`")
    lines.append(f"- max_tokens:  `{args.max_tokens}`")
    lines.append(f"- request_cap: `{args.request_cap}`")
    lines.append(f"- stream-idle hang threshold: `{args.stream_idle_timeout}s`")
    lines.append("")
    lines.append(
        "Prompt mix per request: 25% short (~512 tok), 50% medium (~4k tok), 25% long (~16k tok)."
    )
    lines.append("")
    lines.append("## Results")
    lines.append("")
    lines.append(
        "| variant         | job       | total | ok | fail | fail_rate | failure_breakdown          | agg tok/s | med tok/s | p95 lat |"
    )
    lines.append(
        "|-----------------|-----------|-------|----|------|-----------|----------------------------|-----------|-----------|---------|"
    )
    for r in results:
        if "summary" not in r:
            lines.append(
                f"| {r['variant']:<15} | {r['job_id']:<9} | — | — | — | — | ERROR: {r['error']} | — | — | — |"
            )
            continue
        s = r["summary"]
        breakdown = (
            ",".join(f"{k}={v}" for k, v in s["failure_breakdown"].items()) or "—"
        )
        lines.append(
            f"| {r['variant']:<15} | {r['job_id']:<9} | "
            f"{s['total_requests']} | {s['successes']} | {s['failures']} | "
            f"{s['failure_rate']:.2%} | {breakdown:<26} | "
            f"{s['aggregate_tok_s']:>9.0f} | {s['median_per_req_tok_s']:>9.0f} | "
            f"{s['p95_latency_s']:>6.1f}s |"
        )
    lines.append("")
    lines.append("## Notes")
    lines.append("")
    lines.append(
        "- `fail_rate` is the headline stability metric. Anything > 1% under "
        "this load is unsafe to ship as a production backend."
    )
    lines.append(
        "- `failure_breakdown`: `http_5xx` = engine-level error (matches the "
        "production `EngineCore encountered an issue` 500s); `stream_idle` = "
        "request hung mid-decode for the configured threshold (matches the "
        "vLLM #41125 / #41483 shm-broadcast hang); `no_output` = stream "
        "completed without tokens (usually max_tokens too small for the "
        "reasoning budget, not a stability problem)."
    )
    lines.append(
        "- `agg tok/s` and `med tok/s` are throughput. They are *only* "
        "comparable across variants with the same prompt mix and "
        "concurrency, which this script enforces."
    )
    summary_path.write_text("\n".join(lines))
    return summary_path


def _parse_jobs_arg(spec: str) -> list[Variant]:
    """Parse ``name=jobid[:description],...`` into a list of Variants.

    Lets you stress-test arbitrary running replicas without needing a
    pp_sweep plan.json, e.g. one-off serve replicas submitted by hand.
    """
    out: list[Variant] = []
    for entry in spec.split(","):
        entry = entry.strip()
        if not entry:
            continue
        name, _, rhs = entry.partition("=")
        if not name or not rhs:
            raise ValueError(f"Bad --jobs entry {entry!r}; expected name=jobid[:desc]")
        job_id, _, desc = rhs.partition(":")
        out.append(Variant(name=name, job_id=job_id, description=desc))
    return out


async def main_async(args: argparse.Namespace) -> int:
    if args.from_pp_sweep:
        sweep_dir_in = Path(args.from_pp_sweep)
        if not sweep_dir_in.exists():
            logger.error("Sweep dir does not exist: %s", sweep_dir_in)
            return 1
        variants = _load_pp_sweep(sweep_dir_in)
        out_root = sweep_dir_in
    elif args.jobs:
        variants = _parse_jobs_arg(args.jobs)
        if not args.out_dir:
            logger.error("--out-dir is required when using --jobs.")
            return 1
        out_root = Path(args.out_dir)
        out_root.mkdir(parents=True, exist_ok=True)
    else:
        logger.error("Either --from-pp-sweep or --jobs is required.")
        return 1

    if args.variants:
        wanted = set(args.variants.split(","))
        variants = [v for v in variants if v.name in wanted]
    if not variants:
        logger.error("No variants selected.")
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_dir = out_root / f"stress_{timestamp}"
    out_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Stress sweep output: %s", out_dir)
    logger.info("Variants: %s", [v.name for v in variants])

    results = await asyncio.gather(
        *[
            _run_one(
                v,
                out_dir,
                args.concurrency,
                args.duration_s,
                args.max_tokens,
                args.stream_idle_timeout,
                args.request_cap,
            )
            for v in variants
        ]
    )

    summary_path = _write_summary(results, out_dir, args)
    (out_dir / "stress_results.json").write_text(json.dumps(results, indent=2))
    logger.info("Summary written: %s", summary_path)
    return 0


def main() -> None:
    p = argparse.ArgumentParser(
        description="Run stress_test.py across a sweep's replicas."
    )
    p.add_argument(
        "--from-pp-sweep",
        default="",
        help="Path to a pp_sweep_<timestamp> directory (uses its plan.json + jobs.json).",
    )
    p.add_argument(
        "--jobs",
        default="",
        help="Direct injection: comma-separated 'name=jobid[:description]' pairs (alternative to --from-pp-sweep).",
    )
    p.add_argument(
        "--out-dir",
        default="",
        help="Required with --jobs: parent dir under which the stress_<ts> folder is created.",
    )
    p.add_argument(
        "--variants",
        default="",
        help="Comma-separated subset of variants; empty = all.",
    )
    p.add_argument("--concurrency", type=int, default=24)
    p.add_argument("--duration-s", type=int, default=600)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument("--request-cap", type=int, default=2000)
    p.add_argument(
        "--stream-idle-timeout",
        type=int,
        default=120,
        help="Per-request hang detector threshold (seconds without an SSE token).",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
