"""Command-line entrypoint for the per-user Mnemosyne LaunchAgent."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import contextmanager
import logging
from logging.handlers import RotatingFileHandler
import os
from pathlib import Path
import signal

import uvicorn

from .app import create_control_app, create_inference_app
from .config import MacConfig, load_config
from .runtime import NativeRuntime, validate_exposure


_APP_SUPPORT = Path.home() / "Library" / "Application Support" / "Mnemosyne"


class _NoSignalServer(uvicorn.Server):
    @contextmanager
    def capture_signals(self):
        yield


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Mnemosyne native macOS service")
    parser.add_argument("command", nargs="?", choices=("serve",), default="serve")
    parser.add_argument(
        "--config",
        default=os.environ.get(
            "MNEMOSYNE_MACOS_CONFIG_PATH", str(_APP_SUPPORT / "config.yaml")
        ),
        help="path to native macOS YAML configuration",
    )
    parser.add_argument(
        "--env",
        default=os.environ.get(
            "MNEMOSYNE_MACOS_ENV_PATH", str(_APP_SUPPORT / ".env")
        ),
        help="path to secret environment file",
    )
    parser.add_argument("--log-level", default="info")
    parser.add_argument("--check-config", action="store_true")
    return parser


def _configure_logging(config: MacConfig, level_name: str) -> None:
    level = getattr(logging, str(level_name).upper(), logging.INFO)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    root = logging.getLogger()
    root.setLevel(level)
    if not root.handlers:
        stream = logging.StreamHandler()
        stream.setFormatter(formatter)
        root.addHandler(stream)
    log_directory = Path(config.paths.log_directory).expanduser()
    log_directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    service_log = RotatingFileHandler(
        log_directory / "service.log",
        maxBytes=10 * 1024 * 1024,
        backupCount=5,
        encoding="utf-8",
    )
    service_log.setFormatter(formatter)
    root.addHandler(service_log)


async def _serve(args: argparse.Namespace, config: MacConfig) -> None:
    runtime = NativeRuntime(config, config_path=args.config, env_path=args.env)
    await runtime.start()
    if runtime.startup_error:
        logging.getLogger("mnemosyne-macos").error(
            "startup reconciliation failed closed: %s", runtime.startup_error
        )

    inference = _NoSignalServer(
        uvicorn.Config(
            create_inference_app(runtime),
            host=config.server.inference_bind,
            port=config.server.inference_port,
            log_level=args.log_level,
            lifespan="off",
        )
    )
    control = _NoSignalServer(
        uvicorn.Config(
            create_control_app(runtime),
            host=config.server.control_bind,
            port=config.server.control_port,
            log_level=args.log_level,
            lifespan="off",
        )
    )
    servers = (inference, control)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    installed_signals: list[signal.Signals] = []
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop_event.set)
            installed_signals.append(signum)
        except (NotImplementedError, RuntimeError):
            pass

    server_tasks = {
        asyncio.create_task(server.serve(), name=f"uvicorn-{server.config.port}")
        for server in servers
    }
    stop_task = asyncio.create_task(stop_event.wait(), name="shutdown-signal")
    try:
        done, _pending = await asyncio.wait(
            server_tasks | {stop_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        for task in done:
            if task is not stop_task:
                task.result()
        for server in servers:
            server.should_exit = True
        await asyncio.gather(*server_tasks)
    finally:
        stop_task.cancel()
        for signum in installed_signals:
            loop.remove_signal_handler(signum)
        for server in servers:
            server.should_exit = True
        await runtime.stop()


def main() -> None:
    args = _parser().parse_args()
    config = load_config(args.config, env_path=args.env)
    validate_exposure(config)
    if args.check_config:
        print(
            f"valid: inference={config.server.inference_port} "
            f"control={config.server.control_port} models={len(config.profiles())}"
        )
        return
    _configure_logging(config, args.log_level)
    asyncio.run(_serve(args, config))


if __name__ == "__main__":
    main()
