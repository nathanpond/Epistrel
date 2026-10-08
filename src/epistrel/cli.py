"""`epistrel` command line: `serve` runs the API."""

from __future__ import annotations

import argparse
import logging
import os
import sys

import uvicorn

from epistrel.config import ConfigError, load_settings

EX_CONFIG = 78


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="epistrel", description="Epistrel Engine")
    sub = parser.add_subparsers(dest="command")
    serve = sub.add_parser("serve", help="run the REST API")
    serve.add_argument("--host", help="override EPISTREL_HOST")
    serve.add_argument("--port", type=int, help="override EPISTREL_PORT")
    return parser


def serve(host: str | None, port: int | None) -> int:
    # Overrides go through the environment so uvicorn's factory sees the same values.
    if host is not None:
        os.environ["EPISTREL_HOST"] = host
    if port is not None:
        os.environ["EPISTREL_PORT"] = str(port)
    try:
        settings = load_settings()
    except ConfigError as exc:
        print("\n".join(exc.lines), file=sys.stderr)
        return EX_CONFIG
    level = "DEBUG" if settings.debug else settings.log_level
    logging.basicConfig(level=level, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
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


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "serve":
        return serve(args.host, args.port)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
