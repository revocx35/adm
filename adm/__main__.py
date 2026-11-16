"""python -m adm: the HTTPS server (UI, API, the agents' MCP endpoint and uploads), the loopback-only listener
that agents behind ADM's SSH tunnels reach, and the job loop (started by the HTTPS app's lifespan)."""

from __future__ import annotations

import asyncio
import logging

import uvicorn

from . import tls
from .settings import Settings
from .web import create_app


async def serve() -> None:
    settings = Settings.from_env()
    files = tls.ensure(settings.data_dir, settings.tls_names, settings.tls_cert, settings.tls_key)
    app = create_app(settings, files)
    public = uvicorn.Server(uvicorn.Config(
        app, host=settings.bind, port=settings.https_port, ssl_certfile=files.cert, ssl_keyfile=files.key,
        proxy_headers=False, server_header=False, log_level="info", timeout_keep_alive=30,
        h11_max_incomplete_event_size=64 * 1024))
    # 127.0.0.1 only: never reachable from the network, only through ADM's own SSH reverse tunnels
    internal = uvicorn.Server(uvicorn.Config(
        app.state.internal, host="127.0.0.1", port=settings.internal_port, proxy_headers=False,
        server_header=False, log_level="warning", timeout_keep_alive=30, lifespan="off"))
    await asyncio.gather(public.serve(), internal.serve())


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "uvicorn.access", "asyncssh"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    asyncio.run(serve())


if __name__ == "__main__":
    main()
