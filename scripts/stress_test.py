#!/usr/bin/env python3
"""Long-context concurrency stress-test for a single vLLM endpoint.

The existing ``benchmark_throughput.py`` is great for short-prompt throughput
curves but it cannot surface the production failure mode we keep hitting on
DeepSeek V4 Pro: ``EngineCore encountered an issue`` 500s and silent
shm-broadcast hangs that only show up under sustained, long-context load
(see vLLM #41125 / #41483 / #40969).

This script complements it with a *stability-first* benchmark:

- Builds 3 prompt buckets (short / medium / long) sized to mirror PhysicsIntern
  reasoning-agent traffic (~0.2k, ~4k, ~16k input tokens).
- Streams every request and aborts any that goes ``--stream-idle-timeout``
  seconds without a new token: the upstream symptom is a hang on a single
  decode step, so streaming idle is the right detector.
- Splits failures into three buckets — ``http_5xx`` (engine-level error),
  ``stream_idle`` (hang), and ``other`` (network / parse) — instead of
  collapsing them all to "failure".
- Drives a long (default ~5 min wall, configurable) sustained sweep at one
  concurrency level so we observe the cumulative behaviour, not just the
  first-N-requests pass rate.

JSON output goes to stdout, human-readable progress + final table to stderr.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import random
import statistics
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

import httpx

PROJECT_ROOT = Path(__file__).resolve().parent.parent

logger = logging.getLogger("stress_test")
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    stream=sys.stderr,
)


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------

# A long, content-heavy chunk. Repeating it lets us scale prompt length without
# hitting prefix-cache fast paths for every variant (we mix in random tokens
# at the head + tail to break naive prefix matching).
_CHUNK = (
    "In quantum field theory on curved spacetime the renormalized "
    "stress-energy tensor of a free scalar field of mass m can be derived "
    "from the effective action via point-splitting regularization. The "
    "trace anomaly contributions arise from the Seeley-DeWitt expansion "
    "coefficients a_2(x,x) and depend on local geometric invariants such "
    "as the Ricci scalar R, the squared Weyl tensor C^{abcd}C_{abcd}, the "
    "Gauss-Bonnet density, and the d'Alembertian of R. For the conformally "
    "coupled massless scalar the conformal anomaly takes the well-known "
    "Duff-Brown-Cassidy form. When extending to interacting theories the "
    "coefficients are renormalization-scheme dependent and one must "
    "carefully track the mixing of operators under the renormalization "
    "group flow. This is particularly subtle in de Sitter space where the "
    "infrared sector receives non-perturbative contributions from "
    "stochastic inflation. "
)


def _make_prompt(target_input_tokens: int, seed: int) -> str:
    """Build a prompt of approximately ``target_input_tokens`` tokens.

    Uses ~4 chars/token as a rough estimate. Adds a unique header/footer per
    request so prefix caching doesn't make every prompt a 1-token prefill.
    """
    rng = random.Random(seed)
    target_chars = target_input_tokens * 4
    body_chars = max(target_chars - 256, 0)
    repeats = max(1, body_chars // len(_CHUNK))
    body = (_CHUNK * repeats)[:body_chars]
    nonce = "".join(rng.choices("abcdef0123456789", k=32))
    header = (
        f"# Stability-test session #{nonce}\n\n"
        "Please carefully read the following physics background, then answer "
        "the question at the end. Be concise and structured.\n\n"
    )
    footer = (
        f"\n\n# Question (session {nonce})\n\n"
        "In one short paragraph (no longer than 6 sentences), summarize "
        "the most important physical mechanism mentioned above and its "
        "implications. Do not repeat the input verbatim."
    )
    return header + body + footer


# ---------------------------------------------------------------------------
# Per-request metrics
# ---------------------------------------------------------------------------


@dataclass
class RequestResult:
    request_id: int
    bucket: str
    target_input_tokens: int
    started_at: float
    elapsed_s: float
    ttft_s: float
    output_tokens: int
    last_token_at: float
    success: bool
    failure_class: str | None = None
    error: str | None = None


@dataclass
class StressResult:
    base_url: str
    model: str
    concurrency: int
    duration_s: float
    requests: list[RequestResult] = field(default_factory=list)


# ---------------------------------------------------------------------------
# Single request
# ---------------------------------------------------------------------------


async def _stream_one(
    client: httpx.AsyncClient,
    url: str,
    model: str,
    prompt: str,
    max_tokens: int,
    request_id: int,
    bucket: str,
    target_input_tokens: int,
    stream_idle_timeout: float,
) -> RequestResult:
    """Send one streaming /v1/chat/completions request.

    Aborts the request if no SSE delta arrives within
    ``stream_idle_timeout`` seconds (treats this as a hang).
    """
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.7,
        "stream": True,
        "stream_options": {"include_usage": True},
    }
    started = time.monotonic()
    last_token = started
    ttft = 0.0
    output_tokens = 0
    first_token = False
    failure_class: str | None = None
    error: str | None = None
    success = False

    try:
        async with client.stream(
            "POST",
            url + "/chat/completions",
            json=payload,
            timeout=httpx.Timeout(
                connect=10.0, read=stream_idle_timeout, write=30.0, pool=10.0
            ),
        ) as resp:
            if resp.status_code >= 500:
                failure_class = "http_5xx"
                error = f"HTTP {resp.status_code}"
                # Drain a small body for diagnostic context.
                try:
                    body = await resp.aread()
                    error = f"HTTP {resp.status_code}: {body[:200].decode(errors='replace')}"
                except Exception:
                    pass
            elif resp.status_code != 200:
                failure_class = "http_4xx"
                error = f"HTTP {resp.status_code}"
            else:
                async for line in resp.aiter_lines():
                    if not line or not line.startswith("data:"):
                        continue
                    chunk = line[5:].strip()
                    if chunk == "[DONE]":
                        break
                    try:
                        msg = json.loads(chunk)
                    except json.JSONDecodeError:
                        continue
                    last_token = time.monotonic()
                    # First-token detection: the deepseek_v4 reasoning parser
                    # routes thinking text into delta.reasoning_content while
                    # final text uses delta.content. Either counts as "model
                    # is producing", which is what TTFT and hang-detection
                    # really care about.
                    if not first_token and msg["choices"]:
                        delta = msg["choices"][0]["delta"]
                        if (delta and "content" in delta and delta["content"]) or (
                            delta
                            and "reasoning_content" in delta
                            and delta["reasoning_content"]
                        ):
                            ttft = last_token - started
                            first_token = True
                    if "usage" in msg and msg["usage"]:
                        output_tokens = msg["usage"]["completion_tokens"]
                if output_tokens > 0:
                    success = True
                    if not first_token:
                        # Stream completed before any visible delta — usually
                        # means the engine emitted the whole response in a
                        # single final chunk. Still a "success", just no TTFT.
                        ttft = time.monotonic() - started
                else:
                    failure_class = "no_output"
                    error = "stream completed without tokens"
    except httpx.ReadTimeout:
        failure_class = "stream_idle"
        error = f"no token in {stream_idle_timeout}s"
    except httpx.HTTPError as exc:
        failure_class = "http_error"
        error = f"{type(exc).__name__}: {exc}"
    except Exception as exc:  # noqa: BLE001 — catch all, classify as other
        failure_class = "other"
        error = f"{type(exc).__name__}: {exc}"

    elapsed = time.monotonic() - started
    return RequestResult(
        request_id=request_id,
        bucket=bucket,
        target_input_tokens=target_input_tokens,
        started_at=started,
        elapsed_s=round(elapsed, 3),
        ttft_s=round(ttft, 3),
        output_tokens=output_tokens,
        last_token_at=round(last_token - started, 3),
        success=success,
        failure_class=failure_class,
        error=error,
    )


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


async def run_stress(
    base_url: str,
    model: str,
    concurrency: int,
    duration_s: float,
    max_tokens: int,
    buckets: list[tuple[str, int, float]],
    stream_idle_timeout: float,
    request_cap: int,
) -> StressResult:
    """Sustain ``concurrency`` in-flight requests for ``duration_s`` seconds.

    Each new request draws an input-length bucket according to ``buckets``
    weights. Stops after ``request_cap`` total submissions.
    """
    bucket_names = [b[0] for b in buckets]
    bucket_lengths = {b[0]: b[1] for b in buckets}
    bucket_weights = [b[2] for b in buckets]

    client = httpx.AsyncClient(base_url=base_url)
    rng = random.Random(42)
    sem = asyncio.Semaphore(concurrency)
    submitted = 0
    inflight: list[asyncio.Task[RequestResult]] = []
    results: list[RequestResult] = []
    started = time.monotonic()

    async def _one(req_id: int, bucket: str) -> RequestResult:
        async with sem:
            target = bucket_lengths[bucket]
            prompt = _make_prompt(target, seed=req_id)
            return await _stream_one(
                client,
                base_url,
                model,
                prompt,
                max_tokens,
                req_id,
                bucket,
                target,
                stream_idle_timeout,
            )

    deadline = started + duration_s
    next_log = started + 30
    while time.monotonic() < deadline and submitted < request_cap:
        # Backpressure on the semaphore: open up to concurrency tasks at once.
        if len([t for t in inflight if not t.done()]) >= concurrency:
            done, pending = await asyncio.wait(
                inflight, return_when=asyncio.FIRST_COMPLETED
            )
            for t in done:
                results.append(t.result())
            inflight = list(pending)
            continue
        bucket = rng.choices(bucket_names, weights=bucket_weights, k=1)[0]
        submitted += 1
        inflight.append(asyncio.create_task(_one(submitted, bucket)))

        now = time.monotonic()
        if now >= next_log:
            done_so_far = sum(1 for t in inflight if t.done()) + len(results)
            ok_so_far = sum(1 for r in results if r.success) + sum(
                1 for t in inflight if t.done() and t.result().success
            )
            logger.info(
                "[%s] submitted=%d completed=%d ok=%d inflight=%d elapsed=%.0fs",
                base_url,
                submitted,
                done_so_far,
                ok_so_far,
                sum(1 for t in inflight if not t.done()),
                now - started,
            )
            next_log = now + 30

    # Drain remaining tasks.
    if inflight:
        logger.info("[%s] draining %d inflight tasks", base_url, len(inflight))
        done = await asyncio.gather(*inflight, return_exceptions=False)
        results.extend(done)

    await client.aclose()
    return StressResult(
        base_url=base_url,
        model=model,
        concurrency=concurrency,
        duration_s=round(time.monotonic() - started, 1),
        requests=results,
    )


# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------


def summarize(result: StressResult) -> dict:
    """Aggregate per-request results into a printable summary."""
    total = len(result.requests)
    ok = [r for r in result.requests if r.success]
    fails = [r for r in result.requests if not r.success]
    by_class: dict[str, int] = {}
    for r in fails:
        by_class[r.failure_class or "unknown"] = (
            by_class.get(r.failure_class or "unknown", 0) + 1
        )

    total_out = sum(r.output_tokens for r in ok)
    agg_tps = total_out / result.duration_s if result.duration_s > 0 else 0.0
    if ok:
        latencies = sorted(r.elapsed_s for r in ok)
        ttfts = sorted(r.ttft_s for r in ok)
        per_req_tps = [r.output_tokens / r.elapsed_s for r in ok if r.elapsed_s > 0]
        med_lat = statistics.median(latencies)
        p95_lat = latencies[max(0, int(0.95 * len(latencies)) - 1)]
        med_ttft = statistics.median(ttfts) if ttfts else 0.0
        p95_ttft = ttfts[max(0, int(0.95 * len(ttfts)) - 1)] if ttfts else 0.0
        med_tps = statistics.median(per_req_tps) if per_req_tps else 0.0
    else:
        med_lat = p95_lat = med_ttft = p95_ttft = med_tps = 0.0

    return {
        "base_url": result.base_url,
        "model": result.model,
        "concurrency": result.concurrency,
        "duration_s": result.duration_s,
        "total_requests": total,
        "successes": len(ok),
        "failures": len(fails),
        "failure_rate": round(len(fails) / total, 3) if total else 0.0,
        "failure_breakdown": by_class,
        "total_output_tokens": total_out,
        "aggregate_tok_s": round(agg_tps, 1),
        "median_per_req_tok_s": round(med_tps, 1),
        "median_latency_s": round(med_lat, 2),
        "p95_latency_s": round(p95_lat, 2),
        "median_ttft_s": round(med_ttft, 2),
        "p95_ttft_s": round(p95_ttft, 2),
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _read_endpoint_env(job_id: str) -> dict[str, str]:
    env_path = PROJECT_ROOT / "serve" / "logs" / "vllm" / job_id / "endpoint.env"
    if not env_path.exists():
        raise FileNotFoundError(env_path)
    out = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip()
    return out


async def main_async(args: argparse.Namespace) -> int:
    if args.serve_job:
        env = _read_endpoint_env(args.serve_job)
        base_url = env["BASE_URL"]
        model = args.model or env["SERVED_MODEL_NAME"]
    else:
        base_url = args.base_url
        model = args.model

    # Buckets: (label, target_input_tokens, weight). Defaults mirror the
    # PhysicsIntern surveyor / researcher / formatter mix observed in
    # workspaces_deepseek_v4_pro_high_20260511 (~1k for surveyor, ~4-8k for
    # researchers, ~16k+ for resumed long contexts).
    buckets: list[tuple[str, int, float]] = [
        ("short", 512, 1.0),
        ("medium", 4096, 2.0),
        ("long", 16384, 1.0),
    ]

    logger.info(
        "Stress-testing %s model=%s concurrency=%d duration=%ds idle_timeout=%ds",
        base_url,
        model,
        args.concurrency,
        args.duration_s,
        args.stream_idle_timeout,
    )

    result = await run_stress(
        base_url=base_url,
        model=model,
        concurrency=args.concurrency,
        duration_s=args.duration_s,
        max_tokens=args.max_tokens,
        buckets=buckets,
        stream_idle_timeout=args.stream_idle_timeout,
        request_cap=args.request_cap,
    )
    summary = summarize(result)
    logger.info(
        "DONE %s ok=%d/%d agg=%.0f tok/s p95_lat=%.1fs failures=%s",
        base_url,
        summary["successes"],
        summary["total_requests"],
        summary["aggregate_tok_s"],
        summary["p95_latency_s"],
        summary["failure_breakdown"],
    )

    payload = {
        "summary": summary,
        # Trim full request log: keep failures verbatim, strip success bodies.
        "failures": [asdict(r) for r in result.requests if not r.success],
        "successes_sample": [asdict(r) for r in result.requests[:5]],
    }
    print(json.dumps(payload, indent=2))
    return 0


def main() -> None:
    p = argparse.ArgumentParser(description="vLLM stability stress test.")
    grp = p.add_mutually_exclusive_group(required=True)
    grp.add_argument(
        "--serve-job", help="Read base URL from serve/logs/vllm/<id>/endpoint.env"
    )
    grp.add_argument("--base-url", help="Explicit base URL (e.g. http://host:8000/v1)")
    p.add_argument(
        "--model", help="Override served model name (auto from endpoint.env)"
    )
    p.add_argument("--concurrency", type=int, default=16)
    p.add_argument("--duration-s", type=float, default=300.0)
    p.add_argument("--request-cap", type=int, default=2000)
    p.add_argument("--max-tokens", type=int, default=4096)
    p.add_argument(
        "--stream-idle-timeout",
        type=float,
        default=120.0,
        help="Abort a request as a hang if no SSE token arrives in this many seconds.",
    )
    args = p.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
