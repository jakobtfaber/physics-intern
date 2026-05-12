#!/usr/bin/env python3
"""Throughput + stability sweep for DeepSeek V4 Pro across PP values.

Submits one vLLM serve replica per (pp, stability-knob) variant in parallel
on idle Slurm nodes, waits for each endpoint to come up, runs a long-context
concurrency sweep with `scripts/benchmark_throughput.py`, and writes one JSON
per variant plus a Markdown summary.

This is the *capacity / throughput* harness; pair it with
`scripts/run_stress_sweep.py` (which calls `scripts/stress_test.py` in
parallel against the same replicas) to exercise stability under realistic
long-context load. Both harnesses share the variants list below.

Stability findings from the 2026-05-12 sweep against this matrix
(full results: `results/serve_bench/deepseek_v4_pro_stability/SUMMARY.md`,
also summarized in the comment block above
`deepseek-ai/DeepSeek-V4-Pro-{high,max}` in `src/physics_intern/models.yaml`):

- ``cudagraph_mode: FULL_AND_PIECEWISE`` is the stable mode on H100 / SM 9.0.
  ``PIECEWISE`` engines soft-hang under load (pp4-piecewise produced one
  decode token in two hours of stress). The upstream ``PIECEWISE``
  recommendation (vLLM #40969 / #41125) is for GB10 / SM 12.x and inverts
  on Hopper.
- ``--max-num-batched-tokens 8192`` is the safe ceiling. ``16384`` doubles
  prefill activations and OOMs GPU 0 at the first concurrent prefill,
  which then cascades into NVLink peer-mapping faults and the
  ``EngineCore encountered an issue`` 500 chain seen in production.
- ``VLLM_RPC_TIMEOUT=600000`` (10 min) is kept as a defensive backstop so
  a stalled engine can't self-kill before the load balancer routes around
  it (matches upstream #41125 advice).

Usage:

    uv run python scripts/run_pp_sweep.py            # full thorough sweep
    uv run python scripts/run_pp_sweep.py --dry-run  # print the plan only
    uv run python scripts/run_pp_sweep.py --variants pp4-base,pp6-base
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import subprocess
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVE_SCRIPT = PROJECT_ROOT / "serve" / "serve.slurm"
BENCHMARK_SCRIPT = PROJECT_ROOT / "scripts" / "benchmark_throughput.py"
ENDPOINT_DIR = PROJECT_ROOT / "serve" / "logs" / "vllm"
RESULTS_ROOT = PROJECT_ROOT / "results" / "serve_bench"

# DeepSeek V4 Pro has a 1.6T-total / 49B-active MoE checkpoint (~960 GiB
# mixed FP4+FP8). Loading it from WekaFS through `--safetensors-load-strategy
# prefetch` typically takes 6-10 min on a warm cache and >25 min cold, so
# health checks need a generous ceiling.
HEALTH_TIMEOUT_S = 3600

# Long-context bench profile chosen to mirror PhysicsIntern eval load:
# reasoning agents emit tens of thousands of tokens per call. We can't run
# 30k+ tokens cheaply, so 8192 is the largest "still tractable" sweep depth.
DEFAULT_CONCURRENCY = "1,4,8,16"
DEFAULT_MAX_TOKENS = 8192
DEFAULT_NUM_REQUESTS = 4

# Conservative idle shutdown so a stuck job auto-cancels after the bench
# wraps up; each variant should finish in <90 min wall time once it's up.
IDLE_SHUTDOWN_S = 14400

logger = logging.getLogger("pp_sweep")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


# ---------------------------------------------------------------------------
# Variant definitions
# ---------------------------------------------------------------------------


@dataclass
class Variant:
    """One sweep cell: a unique (pp, scheduler, cudagraph) combination."""

    name: str
    pp: int
    nodes: int
    max_num_seqs: int
    max_num_batched_tokens: int
    cudagraph_mode: str
    notes: str = ""

    def vllm_overrides(self) -> list[str]:
        """Extra CLI args appended after models.yaml's vllm_args."""
        # The DeepSeek-V4-Pro-high entry already passes
        # --compilation-config '{"cudagraph_mode":"PIECEWISE",...,"benchmark_combo_kernel":false}'
        # so we have to keep the inductor flag and just bump the cudagraph
        # mode + capture cap. argparse keeps the last --compilation-config.
        compile_config = json.dumps(
            {
                "cudagraph_mode": self.cudagraph_mode,
                "max_cudagraph_capture_size": 128,
                "inductor_compile_config": {"benchmark_combo_kernel": False},
            }
        )
        return [
            "--max-num-seqs",
            str(self.max_num_seqs),
            "--max-num-batched-tokens",
            str(self.max_num_batched_tokens),
            "--compilation-config",
            compile_config,
        ]


# Full sweep matrix. The FULL_AND_PIECEWISE variants below now match
# `models.yaml`'s DeepSeek-V4-Pro-{high,max} default after the 2026-05-12
# stability sweep showed that PIECEWISE soft-hangs the engine on H100 and
# FULL_AND_PIECEWISE survives 100% of the same stress load. The PIECEWISE
# variants are kept here as the regression baseline: they are what
# reproduces the production hang in case someone needs to confirm a fix.
# pp4-bench specifically reproduces the *previous* models.yaml default
# (PIECEWISE + max_num_batched_tokens=16384) which crashes within minutes
# under stress.
VARIANTS: list[Variant] = [
    # --- PP=2 ---
    Variant(
        name="pp2-base",
        pp=2,
        nodes=2,
        max_num_seqs=16,
        max_num_batched_tokens=8192,
        cudagraph_mode="FULL_AND_PIECEWISE",
        notes="Smallest stable replica (FAP + 8192 batched).",
    ),
    Variant(
        name="pp2-strict",
        pp=2,
        nodes=2,
        max_num_seqs=8,
        max_num_batched_tokens=8192,
        cudagraph_mode="FULL_AND_PIECEWISE",
        notes="Tighter sequence cap to test if PP=2 is viable with throttling.",
    ),
    Variant(
        name="pp2-piecewise",
        pp=2,
        nodes=2,
        max_num_seqs=16,
        max_num_batched_tokens=8192,
        cudagraph_mode="PIECEWISE",
        notes="PP=2 with the production cudagraph mode for direct comparison.",
    ),
    # --- PP=4 ---
    Variant(
        name="pp4-base",
        pp=4,
        nodes=4,
        max_num_seqs=32,
        max_num_batched_tokens=8192,
        cudagraph_mode="FULL_AND_PIECEWISE",
        notes="PP=4 with FAP + 8192 batched. Matches the new models.yaml default.",
    ),
    Variant(
        name="pp4-bench",
        pp=4,
        nodes=4,
        max_num_seqs=32,
        max_num_batched_tokens=16384,
        cudagraph_mode="PIECEWISE",
        notes="Reproduces the *previous* models.yaml default (PIECEWISE+16384). Crashes within minutes under stress; kept as a regression baseline.",
    ),
    Variant(
        name="pp4-piecewise",
        pp=4,
        nodes=4,
        max_num_seqs=32,
        max_num_batched_tokens=8192,
        cudagraph_mode="PIECEWISE",
        notes="PP=4 PIECEWISE with the same scheduler caps as pp4-base for an apples-to-apples cudagraph-only delta.",
    ),
    # --- PP=6 ---
    Variant(
        name="pp6-base",
        pp=6,
        nodes=6,
        max_num_seqs=64,
        max_num_batched_tokens=8192,
        cudagraph_mode="FULL_AND_PIECEWISE",
        notes="Extra memory headroom; 64 in-flight seqs.",
    ),
    Variant(
        name="pp6-aggressive",
        pp=6,
        nodes=6,
        max_num_seqs=128,
        max_num_batched_tokens=8192,
        cudagraph_mode="FULL_AND_PIECEWISE",
        notes="Push concurrency hard with the most memory-comfortable PP.",
    ),
    Variant(
        name="pp6-piecewise",
        pp=6,
        nodes=6,
        max_num_seqs=64,
        max_num_batched_tokens=8192,
        cudagraph_mode="PIECEWISE",
        notes="PP=6 PIECEWISE; the most-headroom production-style replica.",
    ),
]


# ---------------------------------------------------------------------------
# Slurm submission
# ---------------------------------------------------------------------------


SUBMIT_MAX_ATTEMPTS = 30
SUBMIT_RETRY_BACKOFF_S = 30


def submit_serve_job(
    variant: Variant,
    model: str,
    qos: str,
    extra_env: dict[str, str],
) -> str:
    """Submit one serve.slurm job for ``variant`` and return its Slurm job id.

    Retries transient slurm controller errors (e.g. controller DNS hiccups
    where ``sbatch`` reports ``get_addr_info: getaddrinfo() failed``) so the
    sweep doesn't have to be restarted from scratch.
    """
    cmd: list[str] = [
        str(SERVE_SCRIPT),
        "--model",
        model,
        "--nodes",
        str(variant.nodes),
        "--gpus-per-node",
        "8",
        "--tp",
        "8",
        "--pp",
        str(variant.pp),
        "--qos",
        qos,
        "--idle-shutdown",
        str(IDLE_SHUTDOWN_S),
        "--time",
        "12:00:00",
        "--",
        *variant.vllm_overrides(),
    ]
    env = {**os.environ, **extra_env, "_MULTI_SERVE_PARENT": "1"}
    logger.info("Submitting variant %s: %s", variant.name, " ".join(cmd))

    last_stderr = ""
    last_stdout = ""
    for attempt in range(1, SUBMIT_MAX_ATTEMPTS + 1):
        proc = subprocess.run(
            cmd, capture_output=True, text=True, env=env, cwd=str(PROJECT_ROOT)
        )
        last_stderr = proc.stderr.strip()
        last_stdout = proc.stdout
        if proc.returncode == 0:
            match = re.search(r"Submitted (\d+)", proc.stdout) or re.search(
                r"^(\d{6,})", proc.stdout, re.MULTILINE
            )
            if match:
                job_id = match.group(1)
                logger.info(
                    "  -> %s job=%s (attempt %d)", variant.name, job_id, attempt
                )
                return job_id
            logger.error(
                "Could not parse job id from output (attempt %d): %s",
                attempt,
                proc.stdout,
            )
        else:
            logger.warning(
                "serve.slurm rc=%d on attempt %d/%d for %s: %s",
                proc.returncode,
                attempt,
                SUBMIT_MAX_ATTEMPTS,
                variant.name,
                last_stderr,
            )
        if attempt < SUBMIT_MAX_ATTEMPTS:
            time.sleep(SUBMIT_RETRY_BACKOFF_S)

    raise RuntimeError(
        f"submission failed for {variant.name} after {SUBMIT_MAX_ATTEMPTS} attempts: "
        f"stderr={last_stderr!r} stdout={last_stdout!r}"
    )


# ---------------------------------------------------------------------------
# Endpoint discovery + health
# ---------------------------------------------------------------------------


async def wait_for_endpoint(job_id: str, timeout_s: int) -> str | None:
    """Wait for ``serve/logs/vllm/<job>/endpoint.env`` to appear."""
    env_path = ENDPOINT_DIR / job_id / "endpoint.env"
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if env_path.exists():
            for line in env_path.read_text().splitlines():
                if line.startswith("BASE_URL="):
                    return line.split("=", 1)[1].strip()
        # Bail early if the slurm job died.
        if not await _slurm_alive(job_id):
            logger.error("Job %s left the queue before writing endpoint.env", job_id)
            return None
        await asyncio.sleep(15)
    return None


async def _slurm_alive(job_id: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "squeue",
        "-j",
        job_id,
        "--noheader",
        "--format=%T",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    stdout, _ = await proc.communicate()
    state = stdout.decode().strip().splitlines()
    if not state:
        return False
    return state[0].strip() in {"CONFIGURING", "PENDING", "RUNNING", "COMPLETING"}


async def wait_for_health(base_url: str, job_id: str, timeout_s: int) -> bool:
    """Poll ``/health`` until it returns 200 or the slurm job dies."""
    import httpx

    health_url = base_url.replace("/v1", "/health")
    deadline = time.monotonic() + timeout_s
    last_log = 0.0
    async with httpx.AsyncClient(timeout=10.0) as client:
        while time.monotonic() < deadline:
            try:
                resp = await client.get(health_url)
                if resp.status_code == 200:
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            if not await _slurm_alive(job_id):
                logger.error("Job %s died while waiting for health", job_id)
                return False
            now = time.monotonic()
            if now - last_log > 120:
                logger.info("  job %s health: still waiting...", job_id)
                last_log = now
            await asyncio.sleep(10)
    return False


# ---------------------------------------------------------------------------
# Benchmark execution
# ---------------------------------------------------------------------------


@dataclass
class VariantResult:
    """Final per-variant payload for the summary table."""

    variant: Variant
    job_id: str
    base_url: str | None = None
    healthy: bool = False
    benchmark_json: dict[str, Any] | None = None
    error: str | None = None
    benchmark_seconds: float = 0.0
    benchmark_started_at: str = ""


async def run_benchmark(
    job_id: str,
    output_path: Path,
    concurrency: str,
    max_tokens: int,
    num_requests: int,
) -> dict[str, Any]:
    """Invoke ``benchmark_throughput.py`` and return its parsed JSON."""
    cmd = [
        "uv",
        "run",
        "--no-sync",
        "python",
        str(BENCHMARK_SCRIPT),
        "--serve-job",
        job_id,
        "--concurrency",
        concurrency,
        "--num-requests",
        str(num_requests),
        "--max-tokens",
        str(max_tokens),
    ]
    logger.info("[%s] running benchmark: %s", job_id, " ".join(cmd))
    log_path = output_path.with_suffix(".log")
    with log_path.open("w") as log_fh:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=log_fh,
            cwd=str(PROJECT_ROOT),
        )
        stdout, _ = await proc.communicate()

    if proc.returncode != 0:
        raise RuntimeError(
            f"benchmark exited {proc.returncode}; see {log_path} for details"
        )
    output_path.write_bytes(stdout)
    return json.loads(stdout)


async def drive_variant(
    variant: Variant,
    job_id: str,
    sweep_dir: Path,
    concurrency: str,
    max_tokens: int,
    num_requests: int,
) -> VariantResult:
    """End-to-end driver for one variant: wait → benchmark → return result."""
    result = VariantResult(variant=variant, job_id=job_id)
    base_url = await wait_for_endpoint(job_id, HEALTH_TIMEOUT_S)
    if not base_url:
        result.error = "endpoint.env never appeared"
        return result
    result.base_url = base_url
    logger.info("[%s] endpoint up: %s", variant.name, base_url)

    if not await wait_for_health(base_url, job_id, HEALTH_TIMEOUT_S):
        result.error = "health check failed"
        return result
    result.healthy = True
    logger.info("[%s] HEALTHY — starting benchmark", variant.name)

    output_path = sweep_dir / f"{variant.name}.json"
    started = time.monotonic()
    result.benchmark_started_at = datetime.now().isoformat(timespec="seconds")
    try:
        result.benchmark_json = await run_benchmark(
            job_id, output_path, concurrency, max_tokens, num_requests
        )
    except Exception as exc:  # noqa: BLE001 — surface the error in the summary
        result.error = f"benchmark failed: {exc}"
        logger.error("[%s] %s", variant.name, result.error)
    finally:
        result.benchmark_seconds = round(time.monotonic() - started, 1)
    return result


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def write_summary(
    results: list[VariantResult],
    sweep_dir: Path,
    model: str,
    concurrency: str,
    max_tokens: int,
    num_requests: int,
) -> Path:
    """Render a human-readable Markdown summary across variants."""
    summary_path = sweep_dir / "SUMMARY.md"
    lines: list[str] = []
    lines.append(f"# DeepSeek V4 Pro PP sweep — {sweep_dir.name}")
    lines.append("")
    lines.append(f"- model: `{model}`")
    lines.append(f"- concurrency levels: `{concurrency}`")
    lines.append(f"- max output tokens: `{max_tokens}`")
    lines.append(f"- min requests per level: `{num_requests}`")
    lines.append("- vllm: 0.20.2rc1.dev219+g27ae67636")
    lines.append("")
    lines.append("## Variants")
    lines.append("")
    lines.append(
        "| variant | pp | nodes | max_seqs | batched_toks | cudagraph | job | status |"
    )
    lines.append(
        "|---------|----|-------|----------|--------------|-----------|-----|--------|"
    )
    for r in results:
        v = r.variant
        status = (
            "OK"
            if r.benchmark_json
            else (r.error or ("UNHEALTHY" if not r.healthy else "no data"))
        )
        lines.append(
            f"| {v.name} | {v.pp} | {v.nodes} | {v.max_num_seqs} | "
            f"{v.max_num_batched_tokens} | {v.cudagraph_mode} | {r.job_id} | {status} |"
        )
    lines.append("")
    lines.append("## Throughput (aggregate tok/s) per concurrency")
    lines.append("")
    conc_levels = [int(x) for x in concurrency.split(",")]
    header = "| variant | " + " | ".join(f"c={c}" for c in conc_levels) + " |"
    sep = "|---------|" + "|".join(["-" * 6] * len(conc_levels)) + "|"
    lines.append(header)
    lines.append(sep)
    for r in results:
        if not r.benchmark_json:
            lines.append(
                f"| {r.variant.name} | " + " | ".join(["—"] * len(conc_levels)) + " |"
            )
            continue
        per_conc = {row["concurrency"]: row for row in r.benchmark_json["results"]}
        cells: list[str] = []
        for c in conc_levels:
            row = per_conc[c] if c in per_conc else None
            if row is None:
                cells.append("—")
                continue
            ok = row["successes"]
            tot = row["num_requests"]
            agg = row["aggregate_tok_s"]
            cells.append(f"{agg:.0f} ({ok}/{tot})")
        lines.append(f"| {r.variant.name} | " + " | ".join(cells) + " |")
    lines.append("")
    lines.append("## Latency (median / p95) per concurrency")
    lines.append("")
    header = "| variant | " + " | ".join(f"c={c}" for c in conc_levels) + " |"
    lines.append(header)
    lines.append(sep)
    for r in results:
        if not r.benchmark_json:
            lines.append(
                f"| {r.variant.name} | " + " | ".join(["—"] * len(conc_levels)) + " |"
            )
            continue
        per_conc = {row["concurrency"]: row for row in r.benchmark_json["results"]}
        cells = []
        for c in conc_levels:
            row = per_conc[c] if c in per_conc else None
            if row is None:
                cells.append("—")
                continue
            cells.append(f"{row['median_latency_s']:.1f}/{row['p95_latency_s']:.1f}s")
        lines.append(f"| {r.variant.name} | " + " | ".join(cells) + " |")
    lines.append("")
    summary_path.write_text("\n".join(lines))
    return summary_path


# ---------------------------------------------------------------------------
# Main orchestrator
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    selected = (
        VARIANTS
        if not args.variants
        else [v for v in VARIANTS if v.name in set(args.variants.split(","))]
    )
    if not selected:
        logger.error("No variants selected; available: %s", [v.name for v in VARIANTS])
        return 1

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    sweep_dir = RESULTS_ROOT / f"pp_sweep_{timestamp}"
    sweep_dir.mkdir(parents=True, exist_ok=True)
    logger.info("Sweep output: %s", sweep_dir)

    extra_env = {
        # Buys the engine 10 min before it self-kills on the upstream-known
        # shm hang (vLLM #41125, #41483).
        "VLLM_RPC_TIMEOUT": str(args.rpc_timeout_ms),
        # First DeepSeek V4 Pro load can exceed the default 1800s on warm
        # cache and >> on cold cache.
        "VLLM_ENGINE_READY_TIMEOUT_S": "3600",
    }

    plan_path = sweep_dir / "plan.json"
    plan_payload = {
        "timestamp": timestamp,
        "model": args.model,
        "qos": args.qos,
        "concurrency": args.concurrency,
        "max_tokens": args.max_tokens,
        "num_requests": args.num_requests,
        "extra_env": extra_env,
        "variants": [asdict(v) for v in selected],
    }
    plan_path.write_text(json.dumps(plan_payload, indent=2))
    logger.info("Plan written: %s", plan_path)

    if args.dry_run:
        logger.info("Dry-run requested; not submitting any jobs.")
        return 0

    # 1) Submit all variants in parallel before any of them starts loading
    # weights, so they queue together. Per-variant failures don't kill the
    # sweep; they just appear in the summary as ``submit failed``.
    job_ids: dict[str, str] = {}
    submit_errors: dict[str, str] = {}
    for v in selected:
        try:
            job_ids[v.name] = submit_serve_job(v, args.model, args.qos, extra_env)
        except Exception as exc:  # noqa: BLE001
            logger.error("Submission for %s permanently failed: %s", v.name, exc)
            submit_errors[v.name] = str(exc)
    (sweep_dir / "jobs.json").write_text(json.dumps(job_ids, indent=2))

    # 2) Drive each variant to completion in parallel.
    drives = [
        drive_variant(
            v,
            job_ids[v.name],
            sweep_dir,
            args.concurrency,
            args.max_tokens,
            args.num_requests,
        )
        for v in selected
        if v.name in job_ids
    ]
    drive_results = await asyncio.gather(*drives) if drives else []

    # Stitch in placeholder results for any variant that never made it past
    # submission so the summary table still lists them.
    by_name = {r.variant.name: r for r in drive_results}
    results: list[VariantResult] = []
    for v in selected:
        if v.name in by_name:
            results.append(by_name[v.name])
        else:
            results.append(
                VariantResult(
                    variant=v,
                    job_id="—",
                    error=f"submit failed: {submit_errors[v.name]}",
                )
            )

    summary_path = write_summary(
        results,
        sweep_dir,
        args.model,
        args.concurrency,
        args.max_tokens,
        args.num_requests,
    )
    logger.info("Summary written: %s", summary_path)

    # Always emit a JSON sidecar with the raw VariantResult for follow-ups.
    (sweep_dir / "results.json").write_text(
        json.dumps(
            [
                {
                    "variant": asdict(r.variant),
                    "job_id": r.job_id,
                    "base_url": r.base_url,
                    "healthy": r.healthy,
                    "error": r.error,
                    "benchmark_seconds": r.benchmark_seconds,
                    "benchmark_started_at": r.benchmark_started_at,
                    "benchmark_json": r.benchmark_json,
                }
                for r in results
            ],
            indent=2,
        )
    )
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="DeepSeek V4 Pro PP sweep harness.")
    parser.add_argument(
        "--model",
        default="deepseek-ai/DeepSeek-V4-Pro-high",
        help="Model registry key from src/physics_intern/models.yaml.",
    )
    parser.add_argument(
        "--variants",
        default="",
        help="Comma-separated subset of variant names; empty = run all.",
    )
    parser.add_argument(
        "--concurrency",
        default=DEFAULT_CONCURRENCY,
        help=f"Comma-separated concurrency levels (default: {DEFAULT_CONCURRENCY}).",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=DEFAULT_MAX_TOKENS,
        help=f"Max output tokens per request (default: {DEFAULT_MAX_TOKENS}).",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=DEFAULT_NUM_REQUESTS,
        help=f"Min requests per concurrency level (default: {DEFAULT_NUM_REQUESTS}).",
    )
    parser.add_argument(
        "--qos",
        default="low",
        help="Slurm QOS for the sweep replicas (default: low so they don't preempt prod).",
    )
    parser.add_argument(
        "--rpc-timeout-ms",
        type=int,
        default=600000,
        help="VLLM_RPC_TIMEOUT in ms (default: 600000 = 10 min).",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the plan and exit without submitting any jobs.",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
