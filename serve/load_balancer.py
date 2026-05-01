#!/usr/bin/env python3
"""Simple async load balancer for multiple vLLM serve endpoints.

Round-robins incoming OpenAI-compatible requests across N vLLM replicas.
Exposes a single /v1 endpoint that the eval client connects to.

Usage:
    uv run python serve/load_balancer.py 22099201 22099202 22099203
    uv run python serve/load_balancer.py 22099201 22099202 --port 9000

The script reads each job's endpoint.env to discover head-node URLs, waits
for all replicas to become healthy, then starts proxying.
"""

from __future__ import annotations

import argparse
import asyncio
import itertools
import logging
import sys
from pathlib import Path

import httpx
import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, StreamingResponse
from starlette.routing import Route, Mount

logger = logging.getLogger("load_balancer")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def load_endpoints(job_ids: list[str]) -> list[str]:
    """Read BASE_URL from each job's endpoint.env."""
    urls: list[str] = []
    for jid in job_ids:
        env_path = PROJECT_ROOT / "serve" / "logs" / "vllm" / jid / "endpoint.env"
        if not env_path.exists():
            logger.error("endpoint.env not found: %s", env_path)
            sys.exit(1)
        env = {}
        for line in env_path.read_text().splitlines():
            if "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
        urls.append(env["BASE_URL"])
    return urls


async def wait_for_health(urls: list[str], timeout: int = 14400) -> None:
    """Wait until all endpoints respond to /health."""
    async with httpx.AsyncClient(timeout=5) as client:
        for url in urls:
            health_url = url.replace("/v1", "/health")
            logger.info("Waiting for %s ...", health_url)
            elapsed = 0
            while elapsed < timeout:
                try:
                    resp = await client.get(health_url)
                    if resp.status_code == 200:
                        logger.info("  %s healthy", health_url)
                        break
                except (httpx.ConnectError, httpx.ReadTimeout, httpx.ConnectTimeout):
                    pass
                await asyncio.sleep(5)
                elapsed += 5
                if elapsed % 60 == 0:
                    logger.info("  ...still waiting (%ds)", elapsed)
            else:
                logger.error("Timed out waiting for %s after %ds", health_url, timeout)
                sys.exit(1)


class LoadBalancer:
    """Round-robin proxy across vLLM endpoints."""

    def __init__(self, base_urls: list[str]) -> None:
        self.base_urls = base_urls
        self._cycle = itertools.cycle(base_urls)
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(600.0, connect=30.0))

    def _next_url(self) -> str:
        return next(self._cycle)

    async def proxy(self, request: Request) -> StreamingResponse:
        """Forward a request to the next backend and stream the response."""
        target_base = self._next_url()
        target_url = target_base + request.url.path.removeprefix("/v1")
        if request.url.query:
            target_url += f"?{request.url.query}"

        body = await request.body()
        headers = dict(request.headers)
        headers.pop("host", None)

        backend_request = self._client.build_request(
            method=request.method,
            url=target_url,
            headers=headers,
            content=body,
        )
        backend_response = await self._client.send(backend_request, stream=True)

        return StreamingResponse(
            content=backend_response.aiter_raw(),
            status_code=backend_response.status_code,
            headers=dict(backend_response.headers),
            background=backend_response.aclose,
        )


async def health_check(request: Request) -> PlainTextResponse:
    return PlainTextResponse("OK")


def create_app(base_urls: list[str]) -> Starlette:
    lb = LoadBalancer(base_urls)

    async def catch_all(request: Request):
        return await lb.proxy(request)

    return Starlette(
        routes=[
            Route("/health", health_check),
            Mount("/v1", routes=[Route("/{path:path}", catch_all, methods=["GET", "POST"])]),
        ],
    )


async def run_server(app: Starlette, port: int) -> None:
    config = uvicorn.Config(app, host="0.0.0.0", port=port, log_level="info")
    server = uvicorn.Server(config)
    await server.serve()


async def main_async(args: argparse.Namespace) -> int:
    urls = load_endpoints(args.job_ids)
    logger.info("Backends: %s", urls)

    await wait_for_health(urls)
    logger.info("All %d backends healthy. Starting load balancer on port %d.", len(urls), args.port)

    app = create_app(urls)
    await run_server(app, args.port)
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Load balancer for multiple vLLM replicas.")
    parser.add_argument("job_ids", nargs="+", help="Serve job IDs")
    parser.add_argument("--port", type=int, default=9000, help="Load balancer port (default: 9000)")
    args = parser.parse_args()
    sys.exit(asyncio.run(main_async(args)))


if __name__ == "__main__":
    main()
