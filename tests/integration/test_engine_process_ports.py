"""``EngineProcess``'s child binds its own port and reports it (#468): no port is picked first and
bound later, so another process on the host cannot take it in between. A port that is taken
when the child asks for it (a caller's, or a restart's) costs one more start on a fresh port, and
the harness never talks to whoever holds it."""

from __future__ import annotations

import contextlib
import socket
from collections.abc import Iterator

import httpx2 as httpx
import pytest

from epistrel.testing import faults
from tests.auth_support import CREDENTIAL_A

pytestmark = pytest.mark.integration


@contextlib.contextmanager
def held(port: int = 0) -> Iterator[socket.socket]:
    """A listening socket on ``faults.HOST`` (``port``, or any free one): someone else's."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)  # as a server would (TIME_WAIT)
    try:
        sock.bind((faults.HOST, port))
        sock.listen()
        sock.setblocking(False)
        yield sock
    finally:
        sock.close()


def never_connected(sock: socket.socket) -> bool:
    """Nobody connected to the held socket."""
    try:
        conn, _ = sock.accept()
    except BlockingIOError:
        return True
    conn.close()
    return False


def test_the_child_refuses_a_taken_port_and_binds_a_free_one() -> None:
    """``_bind``: ``None`` on a port another socket holds; port 0 binds a free port."""
    with held() as other:
        assert faults._bind(other.getsockname()[1]) is None
    sock = faults._bind(0)
    assert sock is not None
    try:
        assert sock.getsockname()[1] > 0
    finally:
        sock.close()


async def _healthy(child: faults.EngineProcess) -> None:
    async with httpx.AsyncClient(base_url=child.base_url, timeout=30) as client:
        assert (await client.get("/health")).status_code == 200


async def test_a_taken_port_costs_one_retry(pg_url: str) -> None:
    """A child asked for a port someone holds reports ``address-in-use`` and exits; ``start``
    tries once more on a fresh port, which the child reports and ``base_url`` follows. The same
    on a restart whose port was taken while the engine was down. Complement: a restart whose
    port is still free keeps it, with no retry."""
    child = faults.EngineProcess(pg_url, CREDENTIAL_A, mode="api")
    with held() as first:
        taken = first.getsockname()[1]
        child.port = taken
        async with child:
            assert child.port_retries == 1
            assert child.port not in (0, taken)
            assert child.starts == 1
            await _healthy(child)

            kept = child.port
            await child.restart()
            assert (child.port, child.port_retries, child.starts) == (kept, 1, 2)
            await _healthy(child)

            await child.kill()
            with held(kept) as second:
                await child.start()
                assert child.port_retries == 2
                assert child.port not in (0, kept)
                await _healthy(child)
                assert never_connected(second)
        assert never_connected(first)


async def test_start_gives_up_after_its_attempts(
    pg_url: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Every port taken: ``START_ATTEMPTS`` children, then a ``RuntimeError``."""
    with held() as other:
        taken = other.getsockname()[1]

        async def taken_again(self: faults.EngineProcess) -> bool:
            self.port = taken
            return await spawn(self)

        spawn = faults.EngineProcess._spawn
        monkeypatch.setattr(faults.EngineProcess, "_spawn", taken_again)
        child = faults.EngineProcess(pg_url, CREDENTIAL_A, mode="api")
        try:
            with pytest.raises(RuntimeError, match="no free port"):
                await child.start()
        finally:
            await child.__aexit__(None, None, None)
        assert (child.port_retries, child.starts) == (faults.START_ATTEMPTS, 0)
        assert never_connected(other)
