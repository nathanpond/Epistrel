"""Process-level faults for scenario and chaos tests (#61; #75 extends it).

``EngineProcess`` runs the REST engine in a child process (``python -m epistrel.testing.faults
serve``) against a test database with the stub adapter, so a test can kill it outright
(``SIGKILL``: no shutdown, no rollback hooks, exactly a crash) and start it again on the same
port. Options the child applies to itself before serving:

- ``lease_s``: the idempotency claim lease (``core.idempotency.LEASE_S``), short so a retry
  takes over a crashed original quickly;
- ``pause_before_commit``: every ``turn`` operation prints ``EPISTREL-FAULT before-commit
  <operation id>`` and then waits forever instead of committing, so a test can kill the engine
  *between generation and commit* (S-DOUBLE-SUBMIT (c)).

Nothing in ``create_app`` or the Engine imports this module.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import signal
import socket
import sys
from typing import Any

FAULT_MARKER = "EPISTREL-FAULT"
BEFORE_COMMIT = "before-commit"
HOST = "127.0.0.1"
START_TIMEOUT_S = 30.0
MARKER_TIMEOUT_S = 30.0


def free_port() -> int:
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
    ) -> None:
        self.database_url = database_url
        self.credential = credential
        self.port = port or free_port()
        self.lease_s = lease_s
        self.pause_before_commit = pause_before_commit
        self.stub_latency_s = stub_latency_s
        self.process: asyncio.subprocess.Process | None = None
        self.markers: asyncio.Queue[str] = asyncio.Queue()
        self.starts = 0
        self._reader: asyncio.Task[None] | None = None

    @property
    def base_url(self) -> str:
        return f"http://{HOST}:{self.port}"

    async def start(self) -> None:
        """Start the child and wait until ``/health`` answers."""
        if self.process is not None and self.process.returncode is None:
            raise RuntimeError("the engine process is already running")
        env = {
            **{k: v for k, v in os.environ.items() if not k.upper().startswith("EPISTREL_")},
            "EPISTREL_DATABASE_URL": self.database_url,
            "EPISTREL_SERVICE_CREDENTIALS": self.credential,
            "EPISTREL_MODEL_ADAPTER": "stub",
            "EPISTREL_STUB_LATENCY_S": str(self.stub_latency_s),
            "EPISTREL_LOG_LEVEL": "WARNING",
        }
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
        self.process = await asyncio.create_subprocess_exec(
            *args,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.DEVNULL,
        )
        self._reader = asyncio.create_task(self._read_markers(self.process))
        self.starts += 1
        await self._wait_healthy()

    async def _read_markers(self, process: asyncio.subprocess.Process) -> None:
        assert process.stdout is not None  # noqa: S101 - piped above
        while line := await process.stdout.readline():
            text = line.decode(errors="replace").strip()
            if text.startswith(FAULT_MARKER):
                await self.markers.put(text.removeprefix(FAULT_MARKER).strip())

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

    async def restart(self) -> None:
        await self.kill()
        await self.start()

    async def __aenter__(self) -> EngineProcess:
        await self.start()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.kill()


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


def serve(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="epistrel.testing.faults serve")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--lease-s", type=float, default=1.0)
    parser.add_argument("--pause-before-commit", action="store_true")
    args = parser.parse_args(argv)

    import uvicorn

    from epistrel.api import create_app
    from epistrel.core import idempotency

    idempotency.LEASE_S = args.lease_s
    if args.pause_before_commit:
        _pause_turn_commits()
    uvicorn.run(create_app(), host=HOST, port=args.port, log_level="warning")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if not argv or argv[0] != "serve":
        print("usage: python -m epistrel.testing.faults serve --port N", file=sys.stderr)
        return 2
    return serve(argv[1:])


if __name__ == "__main__":  # pragma: no cover - the child process entry point
    sys.exit(main())


__all__ = ["BEFORE_COMMIT", "FAULT_MARKER", "EngineProcess", "free_port", "main", "serve"]
