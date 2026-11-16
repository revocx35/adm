"""python -m adm: the HTTPS server (UI, API, the agents' MCP endpoint and uploads) and the job loop."""

from __future__ import annotations

import logging

import uvicorn

from . import tls
from .settings import Settings
from .web import create_app


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    for noisy in ("httpx", "uvicorn.access"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    settings = Settings.from_env()
    files = tls.ensure(settings.data_dir, settings.tls_names, settings.tls_cert, settings.tls_key)
    app = create_app(settings, files)
    uvicorn.run(app, host=settings.bind, port=settings.https_port, ssl_certfile=files.cert, ssl_keyfile=files.key,
                proxy_headers=False, server_header=False, log_level="info", timeout_keep_alive=30,
                h11_max_incomplete_event_size=64 * 1024)


if __name__ == "__main__":
    main()
