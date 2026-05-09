"""Tests for dynamic load-balancer backend state."""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from serve.load_balancer import BackendPool, slugify  # noqa: E402
from serve.drain_backends import discover_eval_job  # noqa: E402


def test_slugify_matches_serve_job_names() -> None:
    """Model keys map to the same slug shape used in Slurm job names."""
    assert slugify("deepseek-ai/DeepSeek-V4-Pro") == "deepseek-ai-DeepSeek-V4-Pro"
    assert slugify("zai-org/GLM-5.1-runai") == "zai-org-GLM-5-1-runai"


def test_backend_pool_draining_stops_new_assignments() -> None:
    """Drained backends finish existing work but stop receiving new requests."""

    async def scenario() -> None:
        pool = BackendPool()
        await pool.add("1", "http://one:8000/v1")
        await pool.add("2", "http://two:8000/v1")

        first = await pool.acquire()
        assert first is not None
        await pool.mark_draining(first.job_id, "test")

        seen = []
        for _ in range(4):
            lease = await pool.acquire()
            assert lease is not None
            seen.append(lease.job_id)
            await pool.release(lease.job_id)

        assert seen == ["2", "2", "2", "2"]
        assert await pool.active_count(first.job_id) == 1
        await pool.release(first.job_id)
        assert await pool.active_count(first.job_id) == 0

    asyncio.run(scenario())


def test_backend_pool_deduplicates_reused_urls() -> None:
    """A new Slurm job reusing a URL replaces the stale job entry."""

    async def scenario() -> None:
        pool = BackendPool()
        await pool.add("old", "http://host:8000/v1")
        await pool.add("new", "http://host:8000/v1", source="discovered")

        snapshot = await pool.snapshot()
        assert len(snapshot) == 1
        backend = snapshot[0]
        assert backend["job_id"] == "new"
        assert backend["source"] == "discovered"

    asyncio.run(scenario())


def test_backend_pool_respects_per_backend_active_limit() -> None:
    """A backend at its active request limit should not receive new work."""

    async def scenario() -> None:
        pool = BackendPool(max_active_per_backend=2)
        await pool.add("1", "http://one:8000/v1")

        first = await pool.acquire(timeout=0.01)
        second = await pool.acquire(timeout=0.01)
        assert first is not None
        assert second is not None
        assert first.job_id == "1"
        assert second.job_id == "1"

        saturated = await pool.acquire(timeout=0.01)
        assert saturated is None

        await pool.release(first.job_id)
        next_lease = await pool.acquire(timeout=0.01)
        assert next_lease is not None
        assert next_lease.job_id == "1"

    asyncio.run(scenario())


def test_backend_pool_routes_only_to_backends_with_free_capacity() -> None:
    """Round-robin selection should skip saturated and draining backends."""

    async def scenario() -> None:
        pool = BackendPool(max_active_per_backend=1)
        await pool.add("1", "http://one:8000/v1")
        await pool.add("2", "http://two:8000/v1")
        await pool.add("3", "http://three:8000/v1")
        await pool.mark_draining("3", "test")

        first = await pool.acquire(timeout=0.01)
        second = await pool.acquire(timeout=0.01)
        assert first is not None
        assert second is not None
        assert {first.job_id, second.job_id} == {"1", "2"}

        saturated = await pool.acquire(timeout=0.01)
        assert saturated is None

        await pool.release(first.job_id)
        next_lease = await pool.acquire(timeout=0.01)
        assert next_lease is not None
        assert next_lease.job_id == first.job_id

    asyncio.run(scenario())


def test_backend_pool_acquire_waits_for_released_capacity() -> None:
    """Queued requests should wait inside the load balancer for a free slot."""

    async def scenario() -> None:
        pool = BackendPool(max_active_per_backend=1)
        await pool.add("1", "http://one:8000/v1")

        first = await pool.acquire(timeout=0.01)
        assert first is not None

        waiter = asyncio.create_task(pool.acquire(timeout=1.0))
        await asyncio.sleep(0.05)
        assert not waiter.done()

        await pool.release(first.job_id)
        second = await waiter
        assert second is not None
        assert second.job_id == "1"

    asyncio.run(scenario())


def test_backend_pool_status_reports_queue_and_capacity() -> None:
    """Status should expose enough data to tune adaptive backend capacity."""

    async def scenario() -> None:
        pool = BackendPool(max_active_per_backend=2)
        await pool.add("1", "http://one:8000/v1")
        lease = await pool.acquire(timeout=0.01)
        assert lease is not None

        snapshot = await pool.snapshot()
        assert snapshot == [
            {
                "job_id": "1",
                "url": "http://one:8000/v1",
                "source": "configured",
                "active_requests": 1,
                "max_active_requests": 2,
                "available_request_slots": 1,
                "draining": False,
                "state": "RUNNING",
                "last_error": "",
            }
        ]
        assert await pool.queued_count() == 0
        assert await pool.total_capacity() == 2

    asyncio.run(scenario())


def test_discover_eval_job_requires_explicit_job_when_multiple(monkeypatch) -> None:
    """Ambiguous eval-job discovery asks the operator to pass --eval-job."""

    class Result:
        returncode = 0
        stdout = (
            "111|critpt-physicsintern-deepseek-ai-DeepSeek-V4-Pro|RUNNING\n"
            "222|critpt-physicsintern-deepseek-ai-DeepSeek-V4-Pro|RUNNING\n"
        )
        stderr = ""

    monkeypatch.setenv("USER", "joel")
    monkeypatch.setattr("serve.drain_backends._run_command", lambda cmd: Result())

    try:
        discover_eval_job()
    except RuntimeError as exc:
        assert "--eval-job" in str(exc)
    else:
        raise AssertionError("discover_eval_job should reject ambiguous eval jobs")
