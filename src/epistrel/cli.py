"""`epistrel` command line: `serve --mode all|api|worker` runs the Engine (default ``all``: the
API and the background workers in one process); `worker` is `serve --mode worker`;
`evals …` runs the model-quality eval harness (``epistrel.evals``, #134);
`moderation-secret` prints the secret a media moderation hook verifies Epistrel's requests with
(#105); `backup dump` and `backup verify <dump>` take and check a database backup (#125:
erasure markers are expected gaps, and the dump's erasure ledger is checked against the file);
`images reconcile [--fix]` compares the image limit counters with their reservation records and,
with ``--fix``, rewrites the drifted ones (#262)."""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
from typing import TYPE_CHECKING

import uvicorn

from epistrel.config import PROCESS_MODES, ConfigError, ProcessMode, Settings, load_settings
from epistrel.observability import configure_process
from epistrel.tools import backup as backup_tool

if TYPE_CHECKING:
    from fastapi import FastAPI

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
    backup_tool.add_parser(sub)
    evals = sub.add_parser(
        "evals",
        add_help=False,
        help="the model-quality evals: `epistrel evals run|index|--check …` "
        "(`epistrel evals --help` for the options)",
    )
    # Its arguments are the eval CLI's own; ``main`` hands them over before parsing.
    evals.add_argument("evals_args", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    images = sub.add_parser("images", help="image accounting: `epistrel images reconcile [--fix]`")
    image_actions = images.add_subparsers(dest="images_command")
    reconciling = image_actions.add_parser(
        "reconcile",
        help="compare the image limit counters with their reservation records (exit 1 on "
        "drift); --fix rewrites drifted counters and releases stuck held records, with an "
        "audit row",
    )
    reconciling.add_argument("--fix", action="store_true", help="rewrite what drifted")
    models = sub.add_parser(
        "models", help="model configuration: `epistrel models fingerprint --purpose image_check`"
    )
    model_actions = models.add_subparsers(dest="models_command")
    fingerprinting = model_actions.add_parser(
        "fingerprint",
        help="print the configured image checker's fingerprint (#270: the key its E-11 "
        "certifications are recorded under); exit 1 when no image_check model is configured",
    )
    fingerprinting.add_argument("--purpose", choices=("image_check",), default="image_check")
    sub.add_parser(
        "moderation-secret",
        help="print the secret a media moderation hook verifies X-Epistrel-Signature with "
        "(derived from EPISTREL_CONFIG_KEY, which it never reveals)",
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
    return settings


def app_factory() -> FastAPI:
    """uvicorn's factory: this process's logging and tracing (JSON logs through the content
    filter, OTLP when configured; #74), then the app, with the eval harness installed for
    certification runs (#233). A ``--reload`` child runs it too. Logging is configured here,
    after the pre-flight checks, so a refused start changes nothing."""
    from epistrel.api.app import create_app
    from epistrel.evals.certify import install

    settings = load_settings()
    configure_process(settings)
    app = create_app(settings)
    # #233: the certification runs' eval harness, installed here (the composition root): no
    # module of the engine imports the harness.
    install(app.state.epistrel)
    return app


async def _preflight(settings: Settings) -> str | None:
    """Start-up checks: the media root is writable and, on S3, the bucket exists (#105, #107);
    the erasure ledger's directory is writable and its file's chain intact (#125); then those
    that need the database: the media are in the store ``EPISTREL_MEDIA_STORE`` names (#107)
    and a stored model API key has its config key; ``None`` when the server may start."""
    from epistrel.core.db import make_engine
    from epistrel.core.erasure.ledger import LedgerFile, ledger_problem
    from epistrel.core.media.store import MediaRootUnwritable
    from epistrel.core.media.stores import make_store, store_kind_problem
    from epistrel.orchestrator.models.config import config_key_problem

    try:
        await asyncio.to_thread(make_store(settings).prepare)
    except MediaRootUnwritable as exc:
        return str(exc)
    ledger = await asyncio.to_thread(ledger_problem, LedgerFile(settings.erasure_ledger_dir))
    if ledger is not None:
        return ledger
    engine = make_engine(settings)
    try:
        problem = await store_kind_problem(settings, engine)
        if problem is not None:
            return problem
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
        "epistrel.cli:app_factory",
        factory=True,
        host=settings.host,
        port=settings.port,
        log_level=level.lower(),
        # Our handler renders every logger (uvicorn's included); the telemetry middleware writes
        # the one line per request, so uvicorn's access log stays off (#74).
        log_config=None,
        access_log=False,
        reload=settings.debug,
        reload_dirs=["src"] if settings.debug else None,
        workers=1,
    )
    return 0


def moderation_secret() -> int:
    """``epistrel moderation-secret``: the hook secret derived from ``EPISTREL_CONFIG_KEY``
    (``core.media.signing``); exit 78 when the key is unset or malformed."""
    from epistrel.config import ENV_PREFIX, is_fernet_key
    from epistrel.core.media.signing import moderation_secret as derive

    key = os.environ.get(f"{ENV_PREFIX}CONFIG_KEY", "").strip()
    if not is_fernet_key(key):
        print(
            "error: EPISTREL_CONFIG_KEY: must be set to URL-safe base64 of 32 bytes",
            file=sys.stderr,
        )
        return EX_CONFIG
    print(derive(key))
    return 0


def images_reconcile(*, fix: bool) -> int:
    """``epistrel images reconcile [--fix]`` (#262): print the report (JSON); exit 1 when it
    found drift and ``--fix`` was not given, 78 for a configuration error."""
    import json

    from epistrel.core.db import make_engine
    from epistrel.orchestrator.images import reconcile as image_reconcile

    settings = _load()
    if settings is None:
        return EX_CONFIG

    async def run() -> int:
        engine = make_engine(settings)
        try:
            report = await image_reconcile.reconcile(engine)
            found: dict[str, object] = {"report": report.as_json()}
            if fix and not report.clean:
                found["fixed"] = await image_reconcile.fix(engine)
            print(json.dumps(found, indent=2, default=str))
            return 0 if report.clean or fix else 1
        finally:
            await engine.dispose()

    return asyncio.run(run())


def models_fingerprint() -> int:
    """``epistrel models fingerprint --purpose image_check`` (#270): the configured checker's
    fingerprint on its own line; 1 without an ``image_check`` model, 78 for a configuration
    error."""
    from epistrel.core.db import make_engine
    from epistrel.engine import Engine
    from epistrel.orchestrator.images import output_check

    settings = _load()
    if settings is None:
        return EX_CONFIG

    async def run() -> int:
        db = make_engine(settings)
        engine = Engine(settings, db)
        try:
            found = await output_check.checker(engine.models, db)
        finally:
            await engine.models.aclose()
            await db.dispose()
        if found is None:
            print("no image_check model is configured", file=sys.stderr)
            return 1
        print(found.fingerprint)
        return 0

    return asyncio.run(run())


def main(argv: list[str] | None = None) -> int:
    raw = sys.argv[1:] if argv is None else list(argv)
    if raw[:1] == ["evals"]:
        # The eval CLI parses its own arguments (``--help`` and ``--check`` included).
        from epistrel.evals.cli import main as evals_main  # lazy: it loads the whole engine

        return evals_main(raw[1:])
    parser = _build_parser()
    args = parser.parse_args(raw)
    if args.command == "moderation-secret":
        return moderation_secret()
    if args.command == "backup":
        return backup_tool.run(args)
    if args.command == "images":
        if args.images_command == "reconcile":
            return images_reconcile(fix=args.fix)
        print("usage: epistrel images reconcile [--fix]", file=sys.stderr)
        return 2
    if args.command == "models":
        if args.models_command == "fingerprint":
            return models_fingerprint()
        print("usage: epistrel models fingerprint --purpose image_check", file=sys.stderr)
        return 2
    if args.command == "serve":
        return serve(args.host, args.port, args.mode, args.concurrency)
    if args.command == "worker":
        return serve(args.host, args.port, "worker", args.concurrency)
    parser.print_help(sys.stderr)
    return 2


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
