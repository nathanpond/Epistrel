"""`epistrel` command line: `serve --mode all|api|worker` runs the Engine (default ``all``: the
API and the background workers in one process); `worker` is `serve --mode worker`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys

import uvicorn

from epistrel.config import PROCESS_MODES, ConfigError, ProcessMode, Settings, load_settings

EX_CONFIG = 78


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="epistrel", description="Epistrel Engine")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser(
        "serve",
        help="run the Engine: the API and the background workers (--mode all, the default), "
        "the API only (api), or the workers only (worker)",
    )
    serve.add_argument("--host", help="override EPISTREL_HOST")
    serve.add_argument("--port", type=int, help="override EPISTREL_PORT")
    serve.add_argument("--mode", choices=PROCESS_MODES, help="override EPISTREL_MODE")
    serve.add_argument("--concurrency", type=_positive, help="override EPISTREL_WORKER_CONCURRENCY")
    worker = sub.add_parser(
        "worker", help="alias of `serve --mode worker`: the worker loops, serving only /health"
    )
    worker.add_argument("--host", help="override EPISTREL_HOST")
    worker.add_argument("--port", type=int, help="override EPISTREL_PORT")
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


def serve(
    host: str | None,
    port: int | None,
    mode: ProcessMode | None = None,
    concurrency: int | None = None,
) -> int:
    """``epistrel serve``: uvicorn with one process (``workers=1``). In ``all`` and ``worker``
    modes the app's lifespan runs the job worker as an asyncio task (NFR-OPS-1, #71); SIGTERM
    stops it, letting running jobs finish within their lease."""
    # Overrides go through the environment so uvicorn's factory sees the same values.
    overrides = {
        "EPISTREL_HOST": host,
        "EPISTREL_PORT": None if port is None else str(port),
        "EPISTREL_MODE": mode,
        "EPISTREL_WORKER_CONCURRENCY": None if concurrency is None else str(concurrency),
    }
    for name, value in overrides.items():
        if value is not None:
            os.environ[name] = value
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
        workers=1,
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.host, args.port, args.mode, args.concurrency)
    if args.command == "worker":
        return serve(args.host, args.port, "worker", args.concurrency)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
