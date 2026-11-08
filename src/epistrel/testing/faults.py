"""Process-level faults for scenario and chaos tests (#61; #75 extends it).

``EngineProcess`` runs the REST engine in a child process (``python -m epistrel.testing.faults
serve``) against a test database with the stub adapter, so a test can kill it outright
(``SIGKILL``: no shutdown, no rollback hooks, exactly a crash) and start it again on the same
port. Options the child applies to itself before serving:

- ``lease_s``: the idempotency claim lease (``core.idempotency.LEASE_S``) and the turn slot's
  lease (``core.turn_gate.lease_override_s``, #63), short so a retry takes over a crashed
  original, and the next turn the story's slot, quickly (and ``core.orphans.GRACE_S``, so a
  restarted worker's recovery pass fails what the crash left ``in_flight``);
- ``pause_before_commit``: every ``turn`` operation prints ``EPISTREL-FAULT before-commit
  <operation id>`` and then waits forever instead of committing, so a test can kill the engine
  *between generation and commit* (S-DOUBLE-SUBMIT (c)). It may be changed between restarts;
- ``turn_start_delay_s`` (#305): every ``turn`` operation waits this long before it begins (before
  its ``in_flight`` row is written), as on a slow runner where a request takes that long to get
  its turn in flight;
- ``crash`` (#75): a ``crash_at(point, nth, kind)`` plan. The child is started with
  ``EPISTREL_TEST_FAULTS=1`` and ``EPISTREL_TEST_CRASH_AT``, and its ``nth`` hit of the named
  crash point (``epistrel.testing.crash``) ends it with ``os._exit(137)`` after printing
  ``EPISTREL-FAULT crash <point> <kind>``. ``crash`` may be changed between restarts;
- ``mode``: ``EPISTREL_MODE`` (``all`` by default; ``api`` runs no job worker, so a harness
  drains jobs itself);
- ``stub_rules`` (#213): ``StubRule``s the child's stub adapter answers with
  (``StubAdapter.when``), so a child plays scripted turns (a staged turn's render declaring an
  effect, its outcome step). They may be changed between restarts;
- ``images`` (#256): the child registers ``testing.images``' PNG adapter as ``IMAGE_ADAPTER``
  (and, #268, its ``async`` and idempotent variants as ``IMAGE_ADAPTER_ASYNC`` and
  ``IMAGE_ADAPTER_IDEMPOTENT``),
  the test target for ``story`` image jobs, and checks that pass every output (so a test can
  enqueue an image job and kill the child at an ``images.*`` crash point); ``config_key``
  (``EPISTREL_CONFIG_KEY``) lets it decrypt the image adapter's key the test configured;
- ``deliver`` (#213): modules the child treats as delivered (``core.profiles.schema.
  DELIVERED``, as the ``states_delivered`` fixture does in-process; ``states`` until #203
  delivers it for real).

A staged turn's provider is an HTTP one (``HttpManifestProvider``) the test process serves, so
its sends survive a kill; its ``provider_allowed_hosts`` is a deployment setting the child reads
from the shared database per request, and its ``confirm_wait_max_seconds`` is in the provider's
manifest: neither needs an option here.

**Its port (#468).** The child binds its listening socket itself before serving and reports the
port it got (``EPISTREL-FAULT listening <port>``), so no port is picked and then bound later by
someone else: the first start asks for port 0 (any free port), and a restart asks for the same
port again, so a client's ``base_url`` stays valid. A child that finds its port taken reports
``EPISTREL-FAULT address-in-use <port>`` and exits; ``start`` then tries again on a fresh port
(``START_ATTEMPTS`` in all, ``port_retries`` counts them), and ``base_url`` follows. Until the
child reports, the harness never talks to the port, so it cannot reach another process that
holds it.

The child's media root (``EPISTREL_MEDIA_ROOT``, #105) and erasure ledger directory
(``EPISTREL_ERASURE_LEDGER_DIR``, #125) are temporary directories of its own, kept across restarts
and removed when the harness's context ends (or the harness is collected), so a child never
writes under the working directory. Without its own ledger directory the child would use the
default ``var/erasure-ledger`` under the repository, which outlives the test session: its start-up
reconcile would load an earlier session's erasures into the test database as pending entries
(``/health`` ``degraded``) and copy this database's erasures into that file for the next one.

In-process faults (#136): ``readset_intersection_disabled()`` turns off the structured read set's
key-against-change-index intersection (``core.readset.INTERSECTION_ENABLED``) for the block, so a
test can run a build without the FR-CONC-12 check and watch the scenario that relies on it fail;
``amendment_proposition_changes_skipped()`` (#236) records amendments' proposition parts without
their ``proposition_changes`` rows (``core.amendments.store.PROPOSITION_CHANGES``).

Nothing in ``create_app`` or the Engine imports this module.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import errno
import json
import os
import shutil
import signal
import socket
import sys
import tempfile
import weakref
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from epistrel.core import readset
from epistrel.testing.crash import (
    COMMIT_POINTS,
    EXIT_STATUS,
    POINTS,
    STAGED_POINTS,
    CrashPlan,
    crash_at,
    hit,
)

FAULT_MARKER = "EPISTREL-FAULT"
BEFORE_COMMIT = "before-commit"
LISTENING = "listening"
"""The child bound its port: ``listening <port>`` (#468)."""
ADDRESS_IN_USE = "address-in-use"
"""The child's port was taken: ``address-in-use <port>``; it exits with ``EX_ADDRESS_IN_USE``."""
EX_ADDRESS_IN_USE = 75
"""The child's exit status when its port was taken (``EX_TEMPFAIL``: try again)."""
HOST = "127.0.0.1"
START_TIMEOUT_S = 30.0
MARKER_TIMEOUT_S = 30.0
START_ATTEMPTS = 3
"""How many children ``start`` runs before giving up on finding a free port (#468)."""


@dataclass(frozen=True, slots=True)
class StubRule:
    """One answer of the child's stub adapter (``StubAdapter.when``): requests (of ``purpose``,
    when given) whose messages hold every one of ``needles`` are answered ``response``."""

    needles: tuple[str, ...]
    response: str
    purpose: str | None = None


class StubRules:
    """Collects ``StubRule``s through the stub's own ``when`` (``tests``' scripting helpers take
    it in place of a stub)."""

    def __init__(self) -> None:
        self.rules: list[StubRule] = []

    def when(
        self, substring: str | tuple[str, ...], response: str, purpose: str | None = None
    ) -> None:
        needles = (substring,) if isinstance(substring, str) else tuple(substring)
        self.rules.append(StubRule(needles, response, purpose))


def free_port() -> int:
    """A port free a moment ago (nothing holds it for the caller: prefer binding port 0)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind((HOST, 0))
        port: int = sock.getsockname()[1]
        return port


class EngineProcess:
    """The engine in a killable child process; see the module docstring."""

    def __init__(
        self,
        database_url: str,
        credential: str,
        *,
        port: int | None = None,
        lease_s: float = 1.0,
        pause_before_commit: bool = False,
        stub_latency_s: float = 0.0,
        turn_start_delay_s: float = 0.0,
        crash: CrashPlan | None = None,
        mode: str = "all",
        stub_rules: Sequence[StubRule] = (),
        deliver: Sequence[str] = (),
        images: bool = False,
        config_key: str | None = None,
    ) -> None:
        self.database_url = database_url
        self.credential = credential
        self.port = port or 0
        """The child's port: 0 until the first start reports one (see the module docstring)."""
        self.lease_s = lease_s
        self.pause_before_commit = pause_before_commit
        self.stub_latency_s = stub_latency_s
        self.turn_start_delay_s = turn_start_delay_s
        self.crash = crash
        self.mode = mode
        self.stub_rules = list(stub_rules)
        self.deliver = list(deliver)
        self.images = images
        self.config_key = config_key
        self.media_root = tempfile.mkdtemp(prefix="epistrel-faults-media-")
        weakref.finalize(self, shutil.rmtree, self.media_root, ignore_errors=True)
        self.erasure_ledger_dir = tempfile.mkdtemp(prefix="epistrel-faults-ledger-")
        weakref.finalize(self, shutil.rmtree, self.erasure_ledger_dir, ignore_errors=True)
        self.process: asyncio.subprocess.Process | None = None
        self.markers: asyncio.Queue[str] = asyncio.Queue()
        self.starts = 0
        self.port_retries = 0
        """Starts tried again on a fresh port because the port asked for was taken (#468)."""
        self._reader: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    def child_env(self) -> dict[str, str]:
        """The child's environment: this process's, without any ``EPISTREL_*`` variable, plus
        the child's own settings (its media root and erasure ledger are its own directories)."""
        return {
            **{k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")},
            "EPISTREL_DATABASE_URL": self.database_url,
            "EPISTREL_SERVICE_CREDENTIALS": self.credential,
            "EPISTREL_MODEL_ADAPTER": "stub",
            "EPISTREL_STUB_LATENCY_S": str(self.stub_latency_s),
            "EPISTREL_LOG_LEVEL": "WARNING",
            "EPISTREL_MODE": self.mode,
            "EPISTREL_MEDIA_ROOT": self.media_root,
            "EPISTREL_ERASURE_LEDGER_DIR": self.erasure_ledger_dir,
            **(self.crash.env() if self.crash is not None else {}),
            **({"EPISTREL_CONFIG_KEY": self.config_key} if self.config_key else {}),
        }

    async def start(self) -> None:
        """Start the child and wait until ``/health`` answers: on the port it last had (any free
        port the first time), or on a fresh one when that port was taken (#468). A child given a
        ``crash`` plan whose crash point fires before ``/health`` answers (a job it claims at
        start-up reaches the point first) has started as planned: ``start`` returns, and the
        caller's ``wait_exit`` sees the crash's exit status (#728)."""
        if self.process is not None and self.process.returncode is None:
            raise RuntimeError("the engine process is already running")
        for _ in range(START_ATTEMPTS):
            spawned = False
            try:
                spawned = await self._spawn()
                if spawned:
                    self.starts += 1
                    await self._wait_healthy()
                    return
            except RuntimeError:
                if await self._crashed_as_planned():
                    if not spawned:
                        self.starts += 1
                    return
                raise
            self.port = 0
            self.port_retries += 1
        raise RuntimeError(f"the engine process found no free port in {START_ATTEMPTS} attempts")

    async def _crashed_as_planned(self) -> bool:
        """Whether the child, given a ``crash`` plan, ended by its crash point (``EXIT_STATUS``)."""
        process = self.process
        if self.crash is None or process is None:
            return False
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), 5.0)
        return process.returncode == EXIT_STATUS

    def _rules_file(self) -> str:
        """The child's ``stub_rules``, written to a file in its own directory."""
        path = Path(self.media_root, "stub-rules.json")
        path.write_text(json.dumps([asdict(r) for r in self.stub_rules]), encoding="utf-8")
        return str(path)

    async def _spawn(self) -> bool:
        """Run a child on ``self.port`` and wait until it bound it (``True``, ``self.port`` the
        port it got) or found it taken (``False``, the child reaped)."""
        env = self.child_env()
        args = [
            sys.executable,
            "-m",
            "epistrel.testing.faults",
            "serve",
            "--port",
            str(self.port),
            "--lease-s",
            str(self.lease_s),
        ]
        if self.pause_before_commit:
            args.append("--pause-before-commit")
        if self.turn_start_delay_s > 0:
            args += ["--turn-start-delay-s", str(self.turn_start_delay_s)]
        if self.stub_rules:
            args += ["--stub-rules", self._rules_file()]
        for module in self.deliver:
            args += ["--deliver", module]
        if self.images:
            args.append("--images")
        bound: asyncio.Future[int | None] = asyncio.get_running_loop().create_future()
        self.process = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read_markers(self.process, bound))
        try:
            port = await asyncio.wait_for(bound, START_TIMEOUT_S)
        except BaseException:
            await self.kill()  # a child that never bound, or one that died first: reaped
            raise
        if port is None:
            await self.process.wait()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._reader, 5.0)
            return False
        self.port = port
        return True

    async def _read_markers(
        self, process: asyncio.subprocess.Process, bound: asyncio.Future[int | None]
    ) -> None:
        assert process.stdout is not None  # noqa: S101 - piped above
        try:
            while line := await process.stdout.readline():
                text = line.decode(errors="replace").strip()
                if not text.startswith(FAULT_MARKER):
                    continue
                marker = text.removeprefix(FAULT_MARKER).strip()
                name, _, value = marker.partition(" ")
                if name in (LISTENING, ADDRESS_IN_USE) and not bound.done():
                    bound.set_result(int(value) if name == LISTENING else None)
                else:
                    await self.markers.put(marker)
        finally:
            if not bound.done():
                bound.set_exception(RuntimeError("the engine process exited during start-up"))

    async def _wait_healthy(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + START_TIMEOUT_S
        while loop.time() < deadline:
            if self.process is None or self.process.returncode is not None:
                raise RuntimeError("the engine process exited during start-up")
            with contextlib.suppress(OSError):
                reader, writer = await asyncio.open_connection(HOST, self.port)
                writer.write(b"GET /health HTTP/1.0\r\nHost: engine\r\n\r\n")
                await writer.drain()
                status = await reader.readline()
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()
                if b" 200 " in status:
                    return
            await asyncio.sleep(0.1)
        raise TimeoutError("the engine process did not become healthy")

    async def next_marker(self) -> str:
        """The next fault marker the child printed (e.g. ``before-commit <operation id>``);
        ``MARKER_TIMEOUT_S`` at most."""
        return await asyncio.wait_for(self.markers.get(), MARKER_TIMEOUT_S)

    async def kill(self) -> None:
        """``SIGKILL`` the child (a crash) and reap it."""
        process = self.process
        if process is None or process.returncode is not None:
            return
        with contextlib.suppress(ProcessLookupError):
            process.send_signal(signal.SIGKILL)
        await process.wait()
        if self._reader is not None:
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._reader, 5.0)

    @property
    def exited(self) -> int | None:
        """The child's exit status once it ended (137 after a crash point fired), else ``None``."""
        return None if self.process is None else self.process.returncode

    async def wait_exit(self, timeout_s: float = 5.0) -> int | None:
        """Wait up to ``timeout_s`` for the child to end; its exit status, or ``None``."""
        process = self.process
        if process is None:
            return None
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(process.wait(), timeout_s)
        return process.returncode

    async def restart(self) -> None:
        await self.kill()
        await self.start()

    async def __aenter__(self) -> EngineProcess:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.kill()
        shutil.rmtree(self.media_root, ignore_errors=True)
        shutil.rmtree(self.erasure_ledger_dir, ignore_errors=True)


# -- the child ---------------------------------------------------------------------------------


def _pause_turn_commits() -> None:
    """Make every ``turn`` operation announce itself and hang instead of committing."""
    from epistrel.core.enums import OperationKind
    from epistrel.core.events.operation import Operation

    original = Operation.commit

    async def paused(self: Operation, *args: Any, **kwargs: Any) -> None:
        if self.kind is OperationKind.TURN:
            print(f"{FAULT_MARKER} {BEFORE_COMMIT} {self.operation_id}", flush=True)
            await asyncio.Event().wait()
        await original(self, *args, **kwargs)

    Operation.commit = paused  # type: ignore[method-assign]


def _delay_turn_starts(delay_s: float) -> None:
    """Make every ``turn`` operation wait ``delay_s`` before it begins (a slow runner)."""
    from epistrel.core.enums import OperationKind
    from epistrel.core.events.operation import Operation

    original = Operation.begin

    async def delayed(self: Operation, *args: Any, **kwargs: Any) -> Any:
        if self.kind is OperationKind.TURN:
            await asyncio.sleep(delay_s)
        return await original(self, *args, **kwargs)

    Operation.begin = delayed  # type: ignore[method-assign]


def _deliver(modules: Sequence[str]) -> None:
    """Treat ``modules`` as delivered with every level they declare (the child's counterpart of
    the ``states_delivered`` fixture)."""
    import types

    from epistrel.core.profiles import schema

    levels = {"states": frozenset({"prompt_only", schema.ON})}
    schema.DELIVERED = types.MappingProxyType(
        {**schema.DELIVERED, **{m: levels[m] for m in modules}}
    )


def _script_stub(app: Any, path: str) -> None:
    """Install the ``StubRule``s at ``path`` on the app's stub adapter."""
    rules = json.loads(Path(path).read_text(encoding="utf-8"))
    stub = app.state.epistrel.models.stub
    for rule in rules:
        stub.when(tuple(rule["needles"]), rule["response"], purpose=rule["purpose"])


IMAGE_ADAPTER = "faults_png"
"""The image adapter name a child started with ``images`` registers (#256)."""
IMAGE_ADAPTER_ASYNC = "faults_png_async"
"""#268: the child's ``async`` PNG adapter (it acknowledges in ``submit``)."""
IMAGE_ADAPTER_IDEMPOTENT = "faults_png_idempotent"
"""#268: the child's ``sync`` PNG adapter that declares ``idempotency_keys``."""


def _images() -> None:
    """Register the test image adapter, target and passing checks in the child (#256)."""
    from epistrel.core.tables.images import ImageJobKind
    from epistrel.orchestrator.images import jobs
    from epistrel.orchestrator.images.registry import register_image_adapter
    from epistrel.testing.images import PngImageGenerator, ScriptedChecks, TestImageTarget

    register_image_adapter(IMAGE_ADAPTER, PngImageGenerator(), replace=True)
    register_image_adapter(IMAGE_ADAPTER_ASYNC, PngImageGenerator(submission="async"), replace=True)
    register_image_adapter(
        IMAGE_ADAPTER_IDEMPOTENT, PngImageGenerator(idempotency_keys=True), replace=True
    )
    jobs.register_target(ImageJobKind.STORY, TestImageTarget())
    jobs.set_image_checks(ScriptedChecks(fail_closed=False))


def serve(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="epistrel.testing.faults serve")
    parser.add_argument("--port", type=int, required=True, help="0: any free port")
    parser.add_argument("--lease-s", type=float, default=1.0)
    parser.add_argument("--pause-before-commit", action="store_true")
    parser.add_argument("--turn-start-delay-s", type=float, default=0.0)
    parser.add_argument("--stub-rules", default=None)
    parser.add_argument("--deliver", action="append", default=[])
    parser.add_argument("--images", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    from epistrel.api import create_app
    from epistrel.core import idempotency, orphans, turn_gate

    idempotency.LEASE_S = args.lease_s
    turn_gate.lease_override_s = args.lease_s
    orphans.GRACE_S = args.lease_s
    if args.pause_before_commit:
        _pause_turn_commits()
    if args.turn_start_delay_s > 0:
        _delay_turn_starts(args.turn_start_delay_s)
    if args.deliver:
        _deliver(args.deliver)
    if args.images:
        _images()
    app = create_app()
    if args.stub_rules:
        _script_stub(app, args.stub_rules)
    sock = _bind(args.port)
    if sock is None:
        print(f"{FAULT_MARKER} {ADDRESS_IN_USE} {args.port}", flush=True)
        return EX_ADDRESS_IN_USE
    port = int(sock.getsockname()[1])
    print(f"{FAULT_MARKER} {LISTENING} {port}", flush=True)
    config = uvicorn.Config(app, host=HOST, port=port, log_level="warning")
    uvicorn.Server(config).run(sockets=[sock])
    return 0


def _bind(port: int) -> socket.socket | None:
    """The child's listening socket on ``port`` (0: any free port), bound as uvicorn binds its
    own (``SO_REUSEADDR``); ``None`` when another socket holds the port (#468)."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((HOST, port))
    except OSError as exc:
        sock.close()
        if exc.errno == errno.EADDRINUSE:
            return None
        raise
    return sock


@contextlib.contextmanager
def readset_intersection_disabled() -> Iterator[None]:
    """Commit structured turns without checking their query keys against the change index."""
    readset.INTERSECTION_ENABLED = False
    try:
        yield
    finally:
        readset.INTERSECTION_ENABLED = True


@contextlib.contextmanager
def amendment_proposition_changes_skipped() -> Iterator[None]:
    """Record amendments' proposition parts without #136's ``proposition_changes`` rows (the
    fault point ``amend.skip_proposition_changes``, #236)."""
    from epistrel.core.amendments import store

    store.PROPOSITION_CHANGES = False
    try:
        yield
    finally:
        store.PROPOSITION_CHANGES = True


@contextlib.contextmanager
def rewrite_apply_failing() -> Iterator[None]:
    """Fail every rewrite apply after its parts are written (the fault point
    ``rewrite.apply.after_part``, #224): the apply's transaction rolls back whole."""
    from epistrel.orchestrator.rewrite import apply

    apply.FAIL_AFTER_PARTS = True
    try:
        yield
    finally:
        apply.FAIL_AFTER_PARTS = False


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "serve":
        print("usage: python -m epistrel.testing.faults serve --port N", file=sys.stderr)
        return 2
    return serve(argv[1:])


if __name__ == "__main__":  # pragma: no cover - the child process entry point
    sys.exit(main())


@contextlib.contextmanager
def rewrite_research_skipped() -> Iterator[None]:
    """Skip the apply revalidation's dependency re-search (the fault point
    ``rewrite.revalidate.skip_research``, #237): a dependent formed since the preview is not
    seen, so a test can watch S-PREVIEW-NEW-DEPENDENT fail without it."""
    from epistrel.orchestrator.rewrite import revalidate

    revalidate.RESEARCH = False
    try:
        yield
    finally:
        revalidate.RESEARCH = True


__all__ = [
    "ADDRESS_IN_USE",
    "BEFORE_COMMIT",
    "COMMIT_POINTS",
    "EX_ADDRESS_IN_USE",
    "FAULT_MARKER",
    "IMAGE_ADAPTER",
    "IMAGE_ADAPTER_ASYNC",
    "IMAGE_ADAPTER_IDEMPOTENT",
    "LISTENING",
    "POINTS",
    "STAGED_POINTS",
    "START_ATTEMPTS",
    "CrashPlan",
    "EngineProcess",
    "StubRule",
    "StubRules",
    "crash_at",
    "free_port",
    "hit",
    "main",
    "readset_intersection_disabled",
    "serve",
]
