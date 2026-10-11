"""`epistrel` command line: `serve` runs the API, `worker` runs the background-job workers."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import signal
import sys

import uvicorn

from epistrel.config import ConfigError, Settings, load_settings

EX_CONFIG = 78


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="epistrel", description="Epistrel Engine")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the REST API")
    serve.add_argument("--host", help="override EPISTREL_HOST")
    serve.add_argument("--port", type=int, help="override EPISTREL_PORT")
    worker = sub.add_parser("worker", help="run the background-job worker loops (NFR-REL-5)")
    worker.add_argument(
        "--concurrency", type=_positive, help="override EPISTREL_WORKER_CONCURRENCY"
    )
    return parser


def _positive(value: str) -> int:
    number = int(value)
    if number < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return number


def _load() -> Settings | None:
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("\n".join(exc.lines), file=sys.stderr)
        return None
    level = "DEBUG" if settings.debug else settings.log_level
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    return settings


async def _preflight(settings: Settings) -> str | None:
    """Checks that need the database; ``None`` when the server may start."""
    from epistrel.core.db import make_engine
    from epistrel.orchestrator.models.config import config_key_problem

    engine = make_engine(settings)
    try:
        return await config_key_problem(settings, engine)
    finally:
        await engine.dispose()


def serve(host: str | None, port: int | None) -> int:
    # Overrides go through the environment so uvicorn's factory sees the same values.
    if host is not None:
        os.environ["EPISTREL_HOST"] = host
    if port is not None:
        os.environ["EPISTREL_PORT"] = str(port)
    settings = _load()
    if settings is None:
        return EX_CONFIG
    level = "DEBUG" if settings.debug else settings.log_level
    problem = asyncio.run(_preflight(settings))
    if problem is not None:
        print(problem, file=sys.stderr)
        return EX_CONFIG
    uvicorn.run(
        "epistrel.api.app:create_app",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=level.lower(),
        reload=settings.debug,
        reload_dirs=["src"] if settings.debug else None,
    )
    return 0


async def _run_worker(settings: Settings, concurrency: int | None) -> None:
    from epistrel.core.db import make_engine
    from epistrel.orchestrator.jobs.worker import Worker, WorkerConfig

    engine = make_engine(settings)
    worker = Worker(engine, WorkerConfig.from_settings(settings, concurrency=concurrency))
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGTERM, signal.SIGINT):
        with contextlib.suppress(NotImplementedError):  # pragma: no cover - not on Windows
            loop.add_signal_handler(signum, worker.stop)
    try:
        await worker.run()
    finally:
        await engine.dispose()


def run_worker(concurrency: int | None) -> int:
    """``epistrel worker``: the recovery pass, then the worker loops until SIGTERM/SIGINT
    (running jobs get ``DRAIN_S`` to finish)."""
    settings = _load()
    if settings is None:
        return EX_CONFIG
    problem = asyncio.run(_preflight(settings))
    if problem is not None:
        print(problem, file=sys.stderr)
        return EX_CONFIG
    asyncio.run(_run_worker(settings, concurrency))
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.host, args.port)
    if args.command == "worker":
        return run_worker(args.concurrency)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
