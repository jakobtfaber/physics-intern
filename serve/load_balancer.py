#!/usr/bin/env python3
"""Resilient async load balancer for multiple vLLM serve endpoints.

Round-robins incoming OpenAI-compatible requests across N vLLM replicas.
Exposes a single /v1 endpoint that the eval client connects to.

Robustness features:
- Starts serving as soon as >=1 backend is healthy (doesn't block on stragglers).
- Checks SLURM job status during health waits to skip dead jobs immediately.
- Periodically monitors backend health and removes dead backends.
- Auto-resubmits failed serve jobs (when --model is provided).
- Deduplicates backend URLs (handles node reuse after job failures).

Usage:
    uv run python serve/load_balancer.py 22099201 22099202 22099203 --port 9000

    # With auto-resubmit on failure:
    uv run python serve/load_balancer.py 22099201 22099202 --port 9000 \
        --model moonshotai/Kimi-K2.6 --nodes-per-replica 4
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import re
import sys
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Mount, Route

logger = logging.getLogger("load_balancer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVE_SCRIPT = PROJECT_ROOT / "serve" / "serve.slurm"
ENDPOINT_DIR = PROJECT_ROOT / "serve" / "logs" / "vllm"


# ---------------------------------------------------------------------------
# SLURM helpers
# ---------------------------------------------------------------------------


async def is_slurm_job_alive(job_id: str) -> bool:
    """Check if a SLURM job is still running or pending."""
    proc = await asyncio.create_subprocess_exec(
        "squeue",
        "-j",
        job_id,
        "--noheader",
        "--format=%T",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, _ = await proc.communicate()
    state = stdout.decode().strip()
    return state in ("RUNNING", "PENDING", "CONFIGURING")


async def resubmit_serve_job(
    model: str,
    nodes_per_replica: int,
    gpus_per_node: int = 8,
    time_limit: str = "48:00:00",
    idle_shutdown: int = 86400,
) -> str | None:
    """Submit a new serve job via serve.slurm; return job ID or None on failure."""
    cmd = [
        str(SERVE_SCRIPT),
        "--model",
        model,
        "--nodes",
        str(nodes_per_replica),
        "--gpus-per-node",
        str(gpus_per_node),
        "--time",
        time_limit,
        "--idle-shutdown",
        str(idle_shutdown),
    ]
    # Clear SLURM env so serve.slurm takes the "outside SLURM" sbatch path,
    # and set _MULTI_SERVE_PARENT to prevent auto-dispatch to multi_serve.sh.
    env = {k: v for k, v in os.environ.items() if not k.startswith("SLURM")}
    env["_MULTI_SERVE_PARENT"] = "1"
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode() + stderr.decode()
    # sbatch --parsable outputs just the job ID; serve.slurm also prints "Submitted <id>".
    match = re.search(r"(\d{6,})", output)
    if match:
        job_id = match.group(1)
        logger.info("Resubmitted serve job: %s", job_id)
        return job_id
    logger.error("Failed to resubmit serve job: %s", output.strip())
    return None


# ---------------------------------------------------------------------------
# Backend pool (thread-safe, dynamic add/remove)
# ---------------------------------------------------------------------------


class BackendPool:
    """Dynamic pool of healthy backend URLs with round-robin dispatch."""

    def __init__(self) -> None:
        self._urls: list[str] = []
        self._lock = asyncio.Lock()
        self._idx: int = 0
        self._ready = asyncio.Event()

    async def add(self, url: str) -> None:
        async with self._lock:
            if url not in self._urls:
                self._urls.append(url)
                logger.info("Pool +backend: %s (pool size: %d)", url, len(self._urls))
                self._ready.set()

    async def remove(self, url: str) -> None:
        async with self._lock:
            if url in self._urls:
                self._urls.remove(url)
                logger.warning(
                    "Pool -backend: %s (pool size: %d)", url, len(self._urls)
                )

    async def next_url(self) -> str | None:
        async with self._lock:
            if not self._urls:
                return None
            url = self._urls[self._idx % len(self._urls)]
            self._idx += 1
            return url

    async def all_urls(self) -> list[str]:
        async with self._lock:
            return list(self._urls)

    async def wait_for_first(self) -> None:
        """Block until at least one backend is available."""
        await self._ready.wait()

    @property
    def size(self) -> int:
        return len(self._urls)


# ---------------------------------------------------------------------------
# Per-slot backend lifecycle manager
# ---------------------------------------------------------------------------


async def _read_endpoint_env(job_id: str) -> str | None:
    """Read BASE_URL from a job's endpoint.env if it exists."""
    env_path = ENDPOINT_DIR / job_id / "endpoint.env"
    if not env_path.exists():
        return None
    env: dict[str, str] = {}
    for line in env_path.read_text().splitlines():
        if "=" in line:
            k, v = line.split("=", 1)
            env[k.strip()] = v.strip()
    return env.get("BASE_URL")


async def _wait_for_endpoint(job_id: str, timeout: int) -> str | None:
    """Wait for endpoint.env to appear; check SLURM status to bail early."""
    elapsed = 0
    while True:
        if elapsed >= timeout:
            logger.error(
                "Timeout (%ds) waiting for endpoint.env of job %s", timeout, job_id
            )
            return None
        url = await _read_endpoint_env(job_id)
        if url:
            logger.info("Job %s endpoint: %s", job_id, url)
            return url
        if not await is_slurm_job_alive(job_id):
            logger.warning("Job %s died before writing endpoint.env", job_id)
            return None
        if elapsed % 60 == 0:
            logger.info("Waiting for endpoint.env of job %s ...", job_id)
        await asyncio.sleep(5)
        elapsed += 5


async def _wait_for_health(job_id: str, url: str, timeout: int) -> bool:
    """Poll a backend until healthy; check SLURM status to bail early."""
    health_url = url.replace("/v1", "/health")
    logger.info("Health-checking %s (job %s)...", health_url, job_id)
    elapsed = 0
    async with httpx.AsyncClient(timeout=5) as client:
        while True:
            if elapsed >= timeout:
                logger.error(
                    "Timeout (%ds) waiting for health of job %s", timeout, job_id
                )
                return False
            try:
                resp = await client.get(health_url)
                if resp.status_code == 200:
                    logger.info("Backend healthy: %s (job %s)", url, job_id)
                    return True
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                pass
            if elapsed > 0 and elapsed % 30 == 0:
                if not await is_slurm_job_alive(job_id):
                    logger.warning("Job %s died during health check", job_id)
                    return False
            await asyncio.sleep(5)
            elapsed += 5
            if elapsed % 60 == 0:
                logger.info("  %s still waiting (%ds)...", health_url, elapsed)


async def manage_backend_slot(
    initial_job_id: str,
    pool: BackendPool,
    health_timeout: int,
    model: str | None = None,
    nodes_per_replica: int = 4,
    max_resubmits: int = 3,
    monitor_interval: int = 60,
) -> None:
    """Manage one backend slot: discover, health-check, monitor, resubmit.

    Each slot independently handles its lifecycle. If the SLURM job dies,
    it is resubmitted up to ``max_resubmits`` times.
    """
    current_jid = initial_job_id
    resubmits = 0

    while True:
        # 1) Wait for the endpoint.env to appear
        url = await _wait_for_endpoint(current_jid, health_timeout)
        if url is None:
            if model and resubmits < max_resubmits:
                new_jid = await resubmit_serve_job(model, nodes_per_replica)
                if new_jid:
                    resubmits += 1
                    logger.info(
                        "Slot resubmit %d/%d: %s -> %s",
                        resubmits,
                        max_resubmits,
                        current_jid,
                        new_jid,
                    )
                    current_jid = new_jid
                    continue
            logger.error("Slot for job %s exhausted (no endpoint)", current_jid)
            return

        # 2) Wait for health
        healthy = await _wait_for_health(current_jid, url, health_timeout)
        if not healthy:
            if model and resubmits < max_resubmits:
                new_jid = await resubmit_serve_job(model, nodes_per_replica)
                if new_jid:
                    resubmits += 1
                    logger.info(
                        "Slot resubmit %d/%d: %s -> %s",
                        resubmits,
                        max_resubmits,
                        current_jid,
                        new_jid,
                    )
                    current_jid = new_jid
                    continue
            logger.error("Slot for job %s exhausted (unhealthy)", current_jid)
            return

        # 3) Add to pool and monitor
        await pool.add(url)
        while await is_slurm_job_alive(current_jid):
            await asyncio.sleep(monitor_interval)

        # 4) Job died during operation — remove and maybe resubmit
        logger.warning("Job %s died while serving", current_jid)
        await pool.remove(url)

        if model and resubmits < max_resubmits:
            new_jid = await resubmit_serve_job(model, nodes_per_replica)
            if new_jid:
                resubmits += 1
                logger.info(
                    "Slot resubmit %d/%d: %s -> %s",
                    resubmits,
                    max_resubmits,
                    current_jid,
                    new_jid,
                )
                current_jid = new_jid
                continue
        logger.error("Slot for job %s exhausted (died while serving)", current_jid)
        return


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------


def create_app(pool: BackendPool) -> Starlette:
    # Max-think responses can stay silent for more than 10 minutes before the
    # backend emits a body chunk, so only bound connection setup and writes.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=None, write=600.0, pool=600.0)
    )

    async def health_check(request: Request) -> PlainTextResponse:
        if pool.size > 0:
            return PlainTextResponse("OK")
        return PlainTextResponse("No healthy backends", status_code=503)

    async def pool_status(request: Request) -> JSONResponse:
        urls = await pool.all_urls()
        return JSONResponse({"backends": urls, "count": len(urls)})

    async def proxy(request: Request) -> StreamingResponse:
        target_base = await pool.next_url()
        if target_base is None:
            return StreamingResponse(
                content=iter([b"No healthy backends"]),
                status_code=503,
            )
        target_url = target_base + request.url.path.removeprefix("/v1")
        if request.url.query:
            target_url += f"?{request.url.query}"

        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)

        backend_request = client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        backend_response = await client.send(backend_request, stream=True)

        return StreamingResponse(
            content=backend_response.aiter_raw(),
            status_code=backend_response.status_code,
            headers=dict(backend_response.headers),
            background=backend_response.aclose,
        )

    return Starlette(
        routes=[
            Route("/health", health_check),
            Route("/status", pool_status),
            Mount(
                "/v1",
                routes=[
                    Route("/{path:path}", proxy, methods=["GET", "POST"]),
                ],
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main_async(args: argparse.Namespace) -> int:
    pool = BackendPool()

    # Launch one manager task per backend slot (all run concurrently).
    slot_tasks = [
        asyncio.create_task(
            manage_backend_slot(
                initial_job_id=jid,
                pool=pool,
                health_timeout=args.health_timeout,
                model=args.model,
                nodes_per_replica=args.nodes_per_replica,
                max_resubmits=args.max_resubmits,
                monitor_interval=args.monitor_interval,
            )
        )
        for jid in args.job_ids
    ]

    # Wait for at least one backend to become healthy before serving.
    logger.info("Waiting for first healthy backend (%d slots)...", len(slot_tasks))
    wait_task = asyncio.create_task(pool.wait_for_first())
    pending: set[asyncio.Task] = {wait_task, *slot_tasks}
    while wait_task in pending:
        done, pending = await asyncio.wait(
            pending,
            return_when=asyncio.FIRST_COMPLETED,
        )
        if wait_task in done:
            break
        # Some slot tasks finished (failed). Keep waiting unless all are done.
        for t in done:
            if t.exception():
                logger.warning("Slot task failed: %s", t.exception())
        if not any(t for t in pending if t is not wait_task):
            logger.error("All backend slots exhausted before any became healthy.")
            return 1

    logger.info(
        "%d backend(s) ready. Starting load balancer on port %d.",
        pool.size,
        args.port,
    )

    app = create_app(pool)
    config = uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resilient load balancer for multiple vLLM replicas."
    )
    parser.add_argument("job_ids", nargs="+", help="SLURM serve job IDs")
    parser.add_argument(
        "--port",
        type=int,
        default=9000,
        help="Load balancer port (default: 9000)",
    )
    parser.add_argument(
        "--health-timeout",
        type=int,
        default=3600,
        help="Per-backend health/endpoint timeout in seconds (default: 3600).",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model key for auto-resubmitting failed serve jobs.",
    )
    parser.add_argument(
        "--nodes-per-replica",
        type=int,
        default=4,
        help="Nodes per replica for resubmitted jobs (default: 4).",
    )
    parser.add_argument(
        "--max-resubmits",
        type=int,
        default=3,
        help="Max resubmit attempts per slot (default: 3).",
    )
    parser.add_argument(
        "--monitor-interval",
        type=int,
        default=60,
        help="Seconds between SLURM liveness checks (default: 60).",
    )
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
