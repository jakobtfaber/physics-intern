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
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path

import httpx

logger = logging.getLogger("load_balancer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SERVE_SCRIPT = PROJECT_ROOT / "serve" / "serve.slurm"
ENDPOINT_DIR = PROJECT_ROOT / "serve" / "logs" / "vllm"
SLURM_PENDING_STATES = {"CONFIGURING", "PENDING"}
SLURM_ALIVE_STATES = {*SLURM_PENDING_STATES, "COMPLETING", "RUNNING"}


# ---------------------------------------------------------------------------
# SLURM helpers
# ---------------------------------------------------------------------------


async def get_slurm_job_state(job_id: str) -> str | None:
    """Return the current Slurm state for a job, or None if it is gone."""
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
    if not state:
        return None
    return state.splitlines()[0].strip()


async def is_slurm_job_alive(job_id: str) -> bool:
    """Check if a Slurm job can still produce or serve an endpoint."""
    state = await get_slurm_job_state(job_id)
    return state in SLURM_ALIVE_STATES


async def cancel_slurm_job_if_alive(job_id: str, reason: str) -> None:
    """Cancel a serve job that the load balancer is about to abandon."""
    state = await get_slurm_job_state(job_id)
    if state not in SLURM_ALIVE_STATES:
        return

    logger.warning(
        "Cancelling serve job %s before resubmit (%s; state=%s)",
        job_id,
        reason,
        state,
    )
    proc = await asyncio.create_subprocess_exec(
        "scancel",
        job_id,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning(
            "Failed to cancel serve job %s: %s",
            job_id,
            stderr.decode().strip(),
        )


async def resubmit_serve_job(
    model: str,
    nodes_per_replica: int,
    gpus_per_node: int = 8,
    time_limit: str = "48:00:00",
    idle_shutdown: int = 7200,
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
# Backend discovery and pool state
# ---------------------------------------------------------------------------


@dataclass
class SlurmJob:
    """Minimal Slurm job metadata needed for backend discovery."""

    job_id: str
    name: str
    state: str


@dataclass
class Backend:
    """One vLLM backend tracked by Slurm job ID."""

    job_id: str
    url: str
    source: str
    active_requests: int = 0
    draining: bool = False
    state: str = "RUNNING"
    last_error: str = ""


@dataclass
class BackendLease:
    """A checked-out backend plus the URL selected for one request."""

    job_id: str
    url: str


def slugify(value: str) -> str:
    """Match serve.slurm's job-name slug for model keys."""
    slug = re.sub(r"[^A-Za-z0-9]+", "-", value).strip("-")
    return re.sub(r"-+", "-", slug)


async def discover_slurm_serve_jobs(model: str | None) -> list[SlurmJob]:
    """Return alive vLLM serve jobs visible in Slurm for this user."""
    proc = await asyncio.create_subprocess_exec(
        "squeue",
        "-u",
        os.environ["USER"],
        "--noheader",
        "--format=%i|%j|%T",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        logger.warning("squeue discovery failed: %s", stderr.decode().strip())
        return []

    expected_suffix = slugify(model) if model else ""
    jobs: list[SlurmJob] = []
    for raw_line in stdout.decode().splitlines():
        parts = raw_line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        job_id, name, state = parts
        if not name.startswith("vllm-serve-"):
            continue
        if expected_suffix and name != f"vllm-serve-{expected_suffix}":
            continue
        if state not in SLURM_ALIVE_STATES:
            continue
        jobs.append(SlurmJob(job_id=job_id, name=name, state=state))
    return jobs


class BackendPool:
    """Dynamic pool of healthy backend URLs with draining and request counts."""

    def __init__(self, max_active_per_backend: int = 0) -> None:
        if max_active_per_backend < 0:
            raise ValueError("max_active_per_backend must be >= 0")
        self._backends: dict[str, Backend] = {}
        self._lock = asyncio.Lock()
        self._capacity_changed = asyncio.Condition(self._lock)
        self._idx: int = 0
        self._ready = asyncio.Event()
        self._max_active_per_backend = max_active_per_backend
        self._queued_requests = 0

    def _has_free_capacity(self, backend: Backend) -> bool:
        if backend.draining:
            return False
        return (
            self._max_active_per_backend == 0
            or backend.active_requests < self._max_active_per_backend
        )

    def _available_ids_locked(self) -> list[str]:
        return [
            job_id
            for job_id, backend in self._backends.items()
            if self._has_free_capacity(backend)
        ]

    async def add(self, job_id: str, url: str, source: str = "configured") -> bool:
        """Add or refresh a backend. Returns True when a new backend joined."""
        async with self._capacity_changed:
            for existing_id, backend in list(self._backends.items()):
                if backend.url == url and existing_id != job_id:
                    del self._backends[existing_id]
                    logger.warning(
                        "Pool -backend: %s job=%s replaced_by=%s",
                        url,
                        existing_id,
                        job_id,
                    )

            if job_id in self._backends:
                backend = self._backends[job_id]
                backend.url = url
                backend.source = source
                backend.state = "RUNNING"
                backend.last_error = ""
                if not backend.draining:
                    self._ready.set()
                    self._capacity_changed.notify_all()
                return False

            self._backends[job_id] = Backend(job_id=job_id, url=url, source=source)
            logger.info(
                "Pool +backend: %s job=%s source=%s (pool size: %d)",
                url,
                job_id,
                source,
                len(self._backends),
            )
            self._ready.set()
            self._capacity_changed.notify_all()
            return True

    async def remove(self, job_id: str, reason: str) -> None:
        async with self._capacity_changed:
            backend = self._backends.pop(job_id, None)
            if backend is None:
                return
            logger.warning(
                "Pool -backend: %s job=%s reason=%s (pool size: %d)",
                backend.url,
                job_id,
                reason,
                len(self._backends),
            )
            self._capacity_changed.notify_all()

    async def mark_draining(self, job_id: str, reason: str) -> bool:
        """Stop assigning new work to a backend while in-flight work drains."""
        async with self._capacity_changed:
            if job_id not in self._backends:
                return False
            backend = self._backends[job_id]
            if not backend.draining:
                backend.draining = True
                backend.last_error = reason
                logger.warning(
                    "Pool draining backend: %s job=%s active=%d reason=%s",
                    backend.url,
                    job_id,
                    backend.active_requests,
                    reason,
                )
                self._capacity_changed.notify_all()
            return True

    async def acquire(self, timeout: float | None = None) -> BackendLease | None:
        """Wait for a backend with free capacity and reserve one request slot."""

        async def _wait_for_capacity() -> BackendLease:
            async with self._capacity_changed:
                self._queued_requests += 1
                try:
                    while True:
                        active_ids = self._available_ids_locked()
                        if active_ids:
                            job_id = active_ids[self._idx % len(active_ids)]
                            self._idx += 1
                            backend = self._backends[job_id]
                            backend.active_requests += 1
                            return BackendLease(job_id=job_id, url=backend.url)
                        await self._capacity_changed.wait()
                finally:
                    self._queued_requests = max(0, self._queued_requests - 1)

        try:
            if timeout is None:
                return await _wait_for_capacity()
            return await asyncio.wait_for(_wait_for_capacity(), timeout=timeout)
        except TimeoutError:
            return None

    async def release(self, job_id: str) -> None:
        async with self._capacity_changed:
            if job_id not in self._backends:
                return
            backend = self._backends[job_id]
            backend.active_requests = max(0, backend.active_requests - 1)
            self._capacity_changed.notify_all()

    async def active_count(self, job_id: str) -> int:
        async with self._lock:
            if job_id not in self._backends:
                return 0
            return self._backends[job_id].active_requests

    async def available_count(self) -> int:
        async with self._lock:
            return sum(1 for backend in self._backends.values() if not backend.draining)

    async def known_job_ids(self) -> set[str]:
        async with self._lock:
            return set(self._backends)

    async def snapshot(self) -> list[dict[str, str | int | bool]]:
        async with self._lock:
            return [
                {
                    "job_id": backend.job_id,
                    "url": backend.url,
                    "source": backend.source,
                    "active_requests": backend.active_requests,
                    "max_active_requests": self._max_active_per_backend,
                    "available_request_slots": (
                        0
                        if backend.draining
                        else (
                            -1
                            if self._max_active_per_backend == 0
                            else max(
                                0,
                                self._max_active_per_backend - backend.active_requests,
                            )
                        )
                    ),
                    "draining": backend.draining,
                    "state": backend.state,
                    "last_error": backend.last_error,
                }
                for backend in self._backends.values()
            ]

    async def queued_count(self) -> int:
        async with self._lock:
            return self._queued_requests

    async def total_capacity(self) -> int:
        async with self._lock:
            if self._max_active_per_backend == 0:
                return -1
            return sum(
                self._max_active_per_backend
                for backend in self._backends.values()
                if not backend.draining
            )

    async def wait_for_first(self) -> None:
        """Block until at least one backend is available."""
        await self._ready.wait()

    @property
    def size(self) -> int:
        return len(self._backends)


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
    """Wait for endpoint.env after Slurm starts the job.

    Replacement jobs can sit pending for hours on a full cluster. Queue time does
    not indicate a bad backend, so the endpoint timeout starts only after Slurm
    reports that the job has left the pending/configuring states.
    """
    endpoint_elapsed = 0
    pending_elapsed = 0
    while True:
        url = await _read_endpoint_env(job_id)
        if url:
            logger.info("Job %s endpoint: %s", job_id, url)
            return url

        state = await get_slurm_job_state(job_id)
        if state not in SLURM_ALIVE_STATES:
            logger.warning(
                "Job %s left the queue before writing endpoint.env (state=%s)",
                job_id,
                state or "unknown",
            )
            return None

        if state in SLURM_PENDING_STATES:
            if pending_elapsed % 300 == 0:
                logger.info(
                    "Job %s is %s; endpoint timeout starts after it runs.",
                    job_id,
                    state,
                )
            await asyncio.sleep(5)
            pending_elapsed += 5
            continue

        if endpoint_elapsed >= timeout:
            logger.error(
                "Timeout (%ds) waiting for endpoint.env of job %s after state=%s",
                timeout,
                job_id,
                state,
            )
            return None
        if endpoint_elapsed % 60 == 0:
            logger.info(
                "Waiting for endpoint.env of job %s (state=%s) ...",
                job_id,
                state,
            )
        await asyncio.sleep(5)
        endpoint_elapsed += 5


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


async def _is_healthy_now(url: str) -> bool:
    """Return whether a backend is healthy right now without waiting."""
    health_url = url.replace("/v1", "/health")
    try:
        async with httpx.AsyncClient(timeout=5) as client:
            resp = await client.get(health_url)
    except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
        return False
    return resp.status_code == 200


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
                await cancel_slurm_job_if_alive(current_jid, "no endpoint")
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
                await cancel_slurm_job_if_alive(current_jid, "unhealthy endpoint")
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
        await pool.add(current_jid, url)
        while await is_slurm_job_alive(current_jid):
            await asyncio.sleep(monitor_interval)

        # 4) Job died during operation — remove and maybe resubmit
        logger.warning("Job %s died while serving", current_jid)
        await pool.remove(current_jid, "slurm job ended")

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


async def discover_backends_loop(
    model: str,
    pool: BackendPool,
    interval: int,
    monitor_interval: int,
) -> None:
    """Periodically discover alive Slurm serve jobs and attach healthy ones."""
    while True:
        jobs = await discover_slurm_serve_jobs(model)
        known = await pool.known_job_ids()
        for job in jobs:
            if job.job_id in known:
                continue
            url = await _read_endpoint_env(job.job_id)
            if url is None:
                continue
            if await _is_healthy_now(url):
                joined = await pool.add(job.job_id, url, source="discovered")
                if joined:
                    asyncio.create_task(
                        monitor_discovered_backend(
                            job.job_id,
                            pool,
                            monitor_interval,
                        )
                    )
        await asyncio.sleep(interval)


async def monitor_discovered_backend(
    job_id: str,
    pool: BackendPool,
    monitor_interval: int,
) -> None:
    """Remove a discovered backend when its Slurm job disappears."""
    while await is_slurm_job_alive(job_id):
        await asyncio.sleep(monitor_interval)
    await pool.remove(job_id, "discovered slurm job ended")


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------


def create_app(pool: BackendPool, queue_timeout: float | None = None):
    """Create the Starlette proxy app.

    Starlette/uvicorn are imported lazily so unit tests can import backend-state
    helpers in the default CI environment, which does not install the local vLLM
    serving extra.
    """
    from starlette.applications import Starlette
    from starlette.requests import Request
    from starlette.responses import JSONResponse, PlainTextResponse, StreamingResponse
    from starlette.routing import Mount, Route

    # Max-think responses can stay silent for more than 10 minutes before the
    # backend emits a body chunk, so only bound connection setup and writes.
    client = httpx.AsyncClient(
        timeout=httpx.Timeout(connect=30.0, read=None, write=600.0, pool=600.0)
    )

    async def health_check(request: Request) -> PlainTextResponse:
        if await pool.available_count() > 0:
            return PlainTextResponse("OK")
        return PlainTextResponse("No healthy backends", status_code=503)

    async def pool_status(request: Request) -> JSONResponse:
        backends = await pool.snapshot()
        return JSONResponse(
            {
                "backends": backends,
                "count": len(backends),
                "queued_requests": await pool.queued_count(),
                "total_capacity": await pool.total_capacity(),
            }
        )

    async def drain_backend(request: Request) -> JSONResponse:
        job_id = request.path_params["job_id"]
        ok = await pool.mark_draining(job_id, "manual drain")
        if not ok:
            return JSONResponse(
                {"error": f"unknown backend job {job_id}"}, status_code=404
            )
        return JSONResponse({"job_id": job_id, "draining": True})

    async def cancel_when_drained(request: Request) -> JSONResponse:
        job_id = request.path_params["job_id"]
        ok = await pool.mark_draining(job_id, "manual drain and cancel")
        if not ok:
            return JSONResponse(
                {"error": f"unknown backend job {job_id}"}, status_code=404
            )

        async def _cancel_after_drain() -> None:
            while await pool.active_count(job_id) > 0:
                await asyncio.sleep(5)
            await cancel_slurm_job_if_alive(job_id, "manual drained scale-down")
            await pool.remove(job_id, "manual drained scale-down")

        asyncio.create_task(_cancel_after_drain())
        return JSONResponse(
            {"job_id": job_id, "draining": True, "cancel": "when_drained"}
        )

    async def proxy(request: Request) -> StreamingResponse:
        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)

        last_error = "No healthy backends"
        max_attempts = max(1, pool.size)
        start_time = asyncio.get_event_loop().time()
        for _ in range(max_attempts):
            if queue_timeout is not None:
                elapsed = asyncio.get_event_loop().time() - start_time
                remaining_timeout = max(0.0, queue_timeout - elapsed)
                if remaining_timeout <= 0:
                    last_error = "Timed out waiting for backend capacity"
                    break
                lease = await pool.acquire(timeout=remaining_timeout)
            else:
                lease = await pool.acquire(timeout=None)
            if lease is None:
                last_error = "Timed out waiting for backend capacity"
                break

            target_url = lease.url + request.url.path.removeprefix("/v1")
            if request.url.query:
                target_url += f"?{request.url.query}"

            backend_request = client.build_request(
                method=request.method,
                url=target_url,
                headers=headers,
                content=body,
            )
            try:
                backend_response = await client.send(backend_request, stream=True)
            except (
                httpx.ConnectError,
                httpx.ConnectTimeout,
                httpx.PoolTimeout,
                httpx.RemoteProtocolError,
            ) as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                await pool.release(lease.job_id)
                await pool.mark_draining(lease.job_id, last_error)
                if await pool.active_count(lease.job_id) == 0:
                    await pool.remove(lease.job_id, last_error)
                continue

            async def stream_response() -> AsyncIterator[bytes]:
                try:
                    async for chunk in backend_response.aiter_raw():
                        yield chunk
                except httpx.HTTPError as exc:
                    await pool.mark_draining(
                        lease.job_id,
                        f"stream error: {type(exc).__name__}: {exc}",
                    )
                    raise
                finally:
                    await backend_response.aclose()
                    await pool.release(lease.job_id)

            return StreamingResponse(
                content=stream_response(),
                status_code=backend_response.status_code,
                headers=dict(backend_response.headers),
            )

        return StreamingResponse(
            content=iter([last_error.encode()]),
            status_code=503,
        )

    return Starlette(
        routes=[
            Route("/health", health_check),
            Route("/status", pool_status),
            Route("/drain/{job_id}", drain_backend, methods=["POST"]),
            Route(
                "/cancel_when_drained/{job_id}", cancel_when_drained, methods=["POST"]
            ),
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
    pool = BackendPool(max_active_per_backend=args.max_active_per_backend)

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
    discovery_task = None
    if args.model and args.discover_interval > 0:
        discovery_task = asyncio.create_task(
            discover_backends_loop(
                args.model,
                pool,
                args.discover_interval,
                args.monitor_interval,
            )
        )
        logger.info(
            "Backend auto-discovery enabled for model %s every %ds.",
            args.model,
            args.discover_interval,
        )

    # Wait for at least one backend to become healthy before serving.
    logger.info(
        "Waiting for first healthy backend (%d configured slots, discovery=%s)...",
        len(slot_tasks),
        "on" if discovery_task else "off",
    )
    wait_task = asyncio.create_task(pool.wait_for_first())
    pending: set[asyncio.Task] = {wait_task, *slot_tasks}
    if discovery_task:
        pending.add(discovery_task)
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

    app = create_app(pool, queue_timeout=args.queue_timeout)
    import uvicorn

    config = uvicorn.Config(app, host="0.0.0.0", port=args.port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Resilient load balancer for multiple vLLM replicas."
    )
    parser.add_argument("job_ids", nargs="*", help="SLURM serve job IDs")
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
    parser.add_argument(
        "--discover-interval",
        type=int,
        default=600,
        help=(
            "Seconds between Slurm scans for new matching vLLM serve jobs "
            "(default: 600; 0 disables)."
        ),
    )
    parser.add_argument(
        "--max-active-per-backend",
        type=int,
        default=0,
        help=(
            "Maximum in-flight proxied requests per backend (default: 0 = unlimited)."
        ),
    )
    parser.add_argument(
        "--queue-timeout",
        type=float,
        default=1800.0,
        help=(
            "Seconds a proxied request may wait for backend capacity (default: 1800)."
        ),
    )
    args = parser.parse_args()
    if not args.job_ids and not args.model:
        parser.error("provide at least one job ID or --model for auto-discovery")
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
