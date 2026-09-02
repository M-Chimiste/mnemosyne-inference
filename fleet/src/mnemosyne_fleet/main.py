from __future__ import annotations

import argparse
import os

import uvicorn

from .app import create_app
from .config import load_config


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Mnemosyne Fleet on Nyx")
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "MNEMOSYNE_FLEET_CONFIG",
            "~/.config/mnemosyne-fleet/config.toml",
        ),
    )
    args = parser.parse_args()
    config = load_config(os.path.expanduser(args.config))
    uvicorn.run(
        create_app(config),
        host=config.server.host,
        port=config.server.port,
        log_config=None,
        # Authentication must see the immediate socket peer. Tailscale Serve
        # legitimately supplies X-Forwarded-For for the remote user, but
        # rewriting ASGI scope["client"] would hide the loopback proxy and
        # make its separately validated identity header fail closed.
        proxy_headers=False,
        # Scheduler reservations and FIFO queues are intentionally
        # process-local in protocol v1. Do not let WEB_CONCURRENCY silently
        # create unsafe independent schedulers.
        workers=1,
    )


if __name__ == "__main__":
    main()
