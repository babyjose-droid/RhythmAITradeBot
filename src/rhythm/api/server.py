"""Entrypoint: ``rhythm-api`` (or ``python -m rhythm.api.server``)."""

from __future__ import annotations

import os

import uvicorn

from rhythm.api.app import create_app


def main() -> None:
    # Bind to loopback by default; containers set RHYTHM_API_HOST=0.0.0.0 and publish the
    # port on 127.0.0.1 only (see docker-compose.yml). Dashboard auth arrives in Phase 8.
    host = os.environ.get("RHYTHM_API_HOST", "127.0.0.1")
    port = int(os.environ.get("RHYTHM_API_PORT", "8000"))
    uvicorn.run(create_app(), host=host, port=port, log_config=None, proxy_headers=False)


if __name__ == "__main__":
    main()
