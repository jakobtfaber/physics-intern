#!/usr/bin/env python3
"""Gracefully drain and cancel vLLM backends through a running load balancer."""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request
from typing import Any


def _run_command(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    """Run a command and return text output."""
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def discover_eval_job() -> str:
    """Return the sole running PhysicsIntern CritPt eval job for this user."""
    user = os.environ["USER"]
    proc = _run_command(
        [
            "squeue",
            "-u",
            user,
            "--noheader",
            "--format=%i|%j|%T",
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(f"squeue failed: {proc.stderr.strip()}")

    matches: list[str] = []
    for line in proc.stdout.splitlines():
        parts = line.strip().split("|", 2)
        if len(parts) != 3:
            continue
        job_id, name, state = parts
        if name.startswith("critpt-physicsintern-") and state == "RUNNING":
            matches.append(job_id)

    if not matches:
        raise RuntimeError("No running critpt-physicsintern eval job found.")
    if len(matches) > 1:
        joined = ", ".join(matches)
        raise RuntimeError(
            f"Multiple running eval jobs found ({joined}); pass --eval-job explicitly."
        )
    return matches[0]


def request_from_eval_job(
    eval_job: str, path: str, method: str = "GET"
) -> dict[str, Any]:
    """Call the load balancer from inside the eval job allocation."""
    code = f"""\
import json
import urllib.request

req = urllib.request.Request("http://localhost:9000{path}", method="{method}")
with urllib.request.urlopen(req, timeout=30) as resp:
    print(resp.read().decode())
"""
    proc = _run_command(
        [
            "srun",
            f"--jobid={eval_job}",
            "--overlap",
            "--ntasks=1",
            "--nodes=1",
            "python",
            "-c",
            code,
        ]
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or proc.stdout.strip())
    return json.loads(proc.stdout)


def main() -> int:
    """Drain and cancel one or more backend Slurm jobs."""
    parser = argparse.ArgumentParser(
        description=(
            "Gracefully drain vLLM backends via the active load balancer, then "
            "cancel each backend job once its in-flight requests finish."
        )
    )
    parser.add_argument(
        "backend_job_ids",
        nargs="+",
        help="Slurm job IDs of vLLM serve backends to drain and cancel.",
    )
    parser.add_argument(
        "--eval-job",
        default=None,
        help="Slurm job ID of the eval job hosting the load balancer.",
    )
    parser.add_argument(
        "--status",
        action="store_true",
        help="Print load-balancer status after submitting drain requests.",
    )
    args = parser.parse_args()

    eval_job = args.eval_job or discover_eval_job()
    print(f"Using eval job {eval_job} for load-balancer access.", file=sys.stderr)

    for backend_job_id in args.backend_job_ids:
        response = request_from_eval_job(
            eval_job,
            f"/cancel_when_drained/{backend_job_id}",
            method="POST",
        )
        print(json.dumps(response, indent=2))

    if args.status:
        status = request_from_eval_job(eval_job, "/status")
        print(json.dumps(status, indent=2))

    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1)
