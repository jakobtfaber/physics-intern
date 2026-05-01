#!/usr/bin/env python3
"""Concurrent throughput benchmark for a vLLM serve endpoint.

Fires N parallel requests at configurable concurrency levels and reports
aggregate tok/s, latency percentiles, and TTFT.

Usage:
    # Using a serve job's endpoint.env:
    uv run python scripts/benchmark_throughput.py --serve-job 22099198

    # Using an explicit base URL:
    uv run python scripts/benchmark_throughput.py --base-url http://10.0.0.1:8000/v1

    # Custom sweep:
    uv run python scripts/benchmark_throughput.py --serve-job 22099198 \
        --concurrency 1,4,8,16,32,64 --max-tokens 512 --num-requests 32
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import json
import statistics
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx
from openai import AsyncOpenAI

PROJECT_ROOT = Path(__file__).resolve().parent.parent

BENCHMARK_PROMPT = (
    "Write a detailed explanation of the theory of general relativity, "
    "covering the equivalence principle, the Einstein field equations, "
    "geodesics, and at least two experimental confirmations."
)


@dataclass
class RequestResult:
    """Metrics from a single request."""

    output_tokens: int
    elapsed_s: float
    ttft_s: float
    success: bool
    error: str | None = None


@dataclass
class ConcurrencyResult:
    """Aggregate metrics for one concurrency level."""

    concurrency: int
    num_requests: int
    total_output_tokens: int
    total_elapsed_s: float
    aggregate_tok_s: float
    per_request_tok_s: float
    median_latency_s: float
    p95_latency_s: float
    median_ttft_s: float
    p95_ttft_s: float
    successes: int
    failures: int
    errors: list[str] = field(default_factory=list)


async def send_request(
    client: AsyncOpenAI,
    model: str,
    max_tokens: int,
    prompt: str,
) -> RequestResult:
    """Send a single streaming request and collect timing metrics."""
    t0 = time.perf_counter()
    ttft = 0.0
    output_tokens = 0
    first_token_seen = False

    try:
        stream = await client.chat.completions.create(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            max_tokens=max_tokens,
            stream=True,
            stream_options={"include_usage": True},
        )
        async for chunk in stream:
            if (
                not first_token_seen
                and chunk.choices
                and chunk.choices[0].delta.content
            ):
                ttft = time.perf_counter() - t0
                first_token_seen = True
            if chunk.usage:
                output_tokens = chunk.usage.completion_tokens

        elapsed = time.perf_counter() - t0

        if output_tokens == 0:
            return RequestResult(
                output_tokens=0,
                elapsed_s=elapsed,
                ttft_s=ttft,
                success=False,
                error="no output tokens reported",
            )

        return RequestResult(
            output_tokens=output_tokens,
            elapsed_s=elapsed,
            ttft_s=ttft if first_token_seen else elapsed,
            success=True,
        )
    except Exception as exc:
        elapsed = time.perf_counter() - t0
        return RequestResult(
            output_tokens=0,
            elapsed_s=elapsed,
            ttft_s=0.0,
            success=False,
            error=f"{type(exc).__name__}: {exc}",
        )


async def run_concurrency_level(
    clients: list[AsyncOpenAI],
    model: str,
    concurrency: int,
    num_requests: int,
    max_tokens: int,
    prompt: str,
) -> ConcurrencyResult:
    """Run num_requests with the given concurrency and collect aggregate stats.

    When multiple clients are provided, requests are round-robined across them.
    """
    semaphore = asyncio.Semaphore(concurrency)
    client_cycle = itertools.cycle(clients)

    async def limited_request() -> RequestResult:
        client = next(client_cycle)
        async with semaphore:
            return await send_request(client, model, max_tokens, prompt)

    t0 = time.perf_counter()
    results = await asyncio.gather(*[limited_request() for _ in range(num_requests)])
    wall_time = time.perf_counter() - t0

    successes = [r for r in results if r.success]
    failures = [r for r in results if not r.success]

    if not successes:
        return ConcurrencyResult(
            concurrency=concurrency,
            num_requests=num_requests,
            total_output_tokens=0,
            total_elapsed_s=wall_time,
            aggregate_tok_s=0.0,
            per_request_tok_s=0.0,
            median_latency_s=0.0,
            p95_latency_s=0.0,
            median_ttft_s=0.0,
            p95_ttft_s=0.0,
            successes=0,
            failures=len(failures),
            errors=[r.error for r in failures if r.error],
        )

    total_tokens = sum(r.output_tokens for r in successes)
    latencies = sorted(r.elapsed_s for r in successes)
    ttfts = sorted(r.ttft_s for r in successes)
    p95_idx = max(0, int(len(latencies) * 0.95) - 1)

    per_req_tps = [r.output_tokens / r.elapsed_s for r in successes if r.elapsed_s > 0]

    return ConcurrencyResult(
        concurrency=concurrency,
        num_requests=num_requests,
        total_output_tokens=total_tokens,
        total_elapsed_s=wall_time,
        aggregate_tok_s=total_tokens / wall_time if wall_time > 0 else 0.0,
        per_request_tok_s=statistics.median(per_req_tps) if per_req_tps else 0.0,
        median_latency_s=statistics.median(latencies),
        p95_latency_s=latencies[p95_idx],
        median_ttft_s=statistics.median(ttfts),
        p95_ttft_s=ttfts[p95_idx],
        successes=len(successes),
        failures=len(failures),
        errors=[r.error for r in failures if r.error][:5],
    )


def _read_endpoint_env(job_id: str) -> dict[str, str]:
    """Parse a serve job's endpoint.env into a dict."""
    env_path = PROJECT_ROOT / "serve" / "logs" / "vllm" / job_id / "endpoint.env"
    if not env_path.exists():
        print(f"endpoint.env not found: {env_path}", file=sys.stderr)
        sys.exit(1)
    env = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env


def resolve_endpoints(args: argparse.Namespace) -> tuple[list[str], str]:
    """Return (list_of_base_urls, model_name) from CLI args."""
    if args.base_url:
        return [args.base_url], args.model or ""

    if not args.serve_job:
        print("Provide --serve-job or --base-url.", file=sys.stderr)
        sys.exit(1)

    base_urls = []
    model = args.model or ""
    for jid in args.serve_job:
        env = _read_endpoint_env(jid)
        base_urls.append(env["BASE_URL"])
        if not model:
            model = (
                env["SERVED_MODEL_NAME"] if "SERVED_MODEL_NAME" in env else env["MODEL"]
            )

    return base_urls, model


def print_results_table(results: list[ConcurrencyResult]) -> None:
    """Print a formatted summary table to stderr."""
    header = (
        f"{'Conc':>5} | {'Reqs':>5} | {'OK':>4} | {'Fail':>4} | "
        f"{'Agg tok/s':>10} | {'Med tok/s':>10} | "
        f"{'Med lat':>8} | {'P95 lat':>8} | "
        f"{'Med TTFT':>9} | {'P95 TTFT':>9}"
    )
    sep = "-" * len(header)
    print(sep, file=sys.stderr)
    print(header, file=sys.stderr)
    print(sep, file=sys.stderr)
    for r in results:
        print(
            f"{r.concurrency:>5} | {r.num_requests:>5} | {r.successes:>4} | {r.failures:>4} | "
            f"{r.aggregate_tok_s:>10.1f} | {r.per_request_tok_s:>10.1f} | "
            f"{r.median_latency_s:>7.2f}s | {r.p95_latency_s:>7.2f}s | "
            f"{r.median_ttft_s:>8.3f}s | {r.p95_ttft_s:>8.3f}s",
            file=sys.stderr,
        )
        if r.errors:
            for e in r.errors[:2]:
                print(f"        error: {e[:120]}", file=sys.stderr)
    print(sep, file=sys.stderr)


async def _wait_for_health(base_url: str, label: str = "") -> bool:
    """Wait for a single endpoint to become healthy."""
    health_url = base_url.replace("/v1", "/health")
    tag = f" ({label})" if label else ""
    print(f"Waiting for {health_url}{tag} ...", file=sys.stderr)
    async with httpx.AsyncClient(timeout=5) as hc:
        for attempt in range(720):
            try:
                resp = await hc.get(health_url)
                if resp.status_code == 200:
                    print(f"  {health_url} healthy.", file=sys.stderr)
                    return True
            except Exception:
                pass
            if attempt % 20 == 0 and attempt > 0:
                print(f"  ...still waiting ({attempt * 5}s)", file=sys.stderr)
            await asyncio.sleep(5)
    print(f"Timed out waiting for {health_url}.", file=sys.stderr)
    return False


async def main_async(args: argparse.Namespace) -> int:
    base_urls, model = resolve_endpoints(args)

    concurrency_levels = [int(c) for c in args.concurrency.split(",")]
    num_requests = args.num_requests

    print(f"Endpoints:    {base_urls}", file=sys.stderr)
    print(f"Replicas:     {len(base_urls)}", file=sys.stderr)
    print(f"Model:        {model}", file=sys.stderr)
    print(f"Max tokens:   {args.max_tokens}", file=sys.stderr)
    print(f"Concurrency:  {concurrency_levels}", file=sys.stderr)
    print(f"Requests/lvl: {num_requests}", file=sys.stderr)
    print("---", file=sys.stderr)

    # Wait for all endpoints to become healthy.
    health_results = await asyncio.gather(
        *[_wait_for_health(url, f"replica {i + 1}") for i, url in enumerate(base_urls)]
    )
    if not all(health_results):
        print("Some endpoints failed to become healthy.", file=sys.stderr)
        return 1

    clients = [AsyncOpenAI(base_url=url, api_key="unused") for url in base_urls]

    # Warmup: one request per replica.
    print("Warmup requests...", file=sys.stderr)
    warmups = await asyncio.gather(
        *[
            send_request(c, model, min(args.max_tokens, 64), BENCHMARK_PROMPT)
            for c in clients
        ]
    )
    for i, w in enumerate(warmups):
        if not w.success:
            print(f"Warmup failed on replica {i + 1}: {w.error}", file=sys.stderr)
            return 1
        print(
            f"  Replica {i + 1}: {w.output_tokens} tokens in {w.elapsed_s:.2f}s",
            file=sys.stderr,
        )

    all_results: list[ConcurrencyResult] = []
    for conc in concurrency_levels:
        actual_requests = max(conc, num_requests)
        print(
            f"\nRunning concurrency={conc}, requests={actual_requests}...",
            file=sys.stderr,
        )
        result = await run_concurrency_level(
            clients, model, conc, actual_requests, args.max_tokens, BENCHMARK_PROMPT
        )
        all_results.append(result)
        print(
            f"  -> {result.aggregate_tok_s:.1f} agg tok/s, "
            f"{result.per_request_tok_s:.1f} med tok/s, "
            f"{result.successes}/{actual_requests} OK",
            file=sys.stderr,
        )

    print("\n", file=sys.stderr)
    print_results_table(all_results)

    # JSON output to stdout for programmatic consumption.
    output = {
        "base_urls": base_urls,
        "num_replicas": len(base_urls),
        "model": model,
        "max_tokens": args.max_tokens,
        "prompt_length": len(BENCHMARK_PROMPT),
        "results": [
            {
                "concurrency": r.concurrency,
                "num_requests": r.num_requests,
                "successes": r.successes,
                "failures": r.failures,
                "total_output_tokens": r.total_output_tokens,
                "total_elapsed_s": round(r.total_elapsed_s, 3),
                "aggregate_tok_s": round(r.aggregate_tok_s, 1),
                "per_request_tok_s": round(r.per_request_tok_s, 1),
                "median_latency_s": round(r.median_latency_s, 3),
                "p95_latency_s": round(r.p95_latency_s, 3),
                "median_ttft_s": round(r.median_ttft_s, 3),
                "p95_ttft_s": round(r.p95_ttft_s, 3),
            }
            for r in all_results
        ],
    }
    print(json.dumps(output, indent=2))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Benchmark vLLM endpoint throughput.")
    parser.add_argument(
        "--serve-job",
        type=str,
        nargs="+",
        help="Serve job ID(s) (reads endpoint.env, multiple for round-robin)",
    )
    parser.add_argument(
        "--base-url", type=str, help="vLLM base URL (e.g. http://host:8000/v1)"
    )
    parser.add_argument(
        "--model", type=str, help="Model name (auto-detected from endpoint.env)"
    )
    parser.add_argument(
        "--concurrency",
        type=str,
        default="1,4,8,16,32,64,128",
        help="Comma-separated concurrency levels (default: 1,4,8,16,32,64,128)",
    )
    parser.add_argument(
        "--num-requests",
        type=int,
        default=32,
        help="Minimum requests per concurrency level (default: 32, actual = max(concurrency, this))",
    )
    parser.add_argument(
        "--max-tokens",
        type=int,
        default=512,
        help="Max tokens per request (default: 512)",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
