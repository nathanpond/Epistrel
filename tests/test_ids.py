"""``new_ulid``'s monotonic mode (#477, NFR-DETERM-1): ids minted in one millisecond sort in
creation order, so an ``ORDER BY id`` tie-break orders rows the same way on two replays; they
stay unique across threads and across a fork; ``random_ulid`` keeps every random bit fresh."""

from __future__ import annotations

import os
import re
import threading

import pytest

from epistrel.core import ids
from epistrel.core.ids import ULID_PATTERN, new_ulid, random_ulid


def test_ids_of_one_millisecond_sort_in_creation_order(frozen_ids: int) -> None:
    minted = [new_ulid() for _ in range(500)]
    assert minted == sorted(minted)
    assert len(set(minted)) == len(minted)
    assert all(re.fullmatch(ULID_PATTERN, i) for i in minted)
    assert {i[:10] for i in minted} == {ids._encode(frozen_ids << 80)[:10]}


def test_a_clock_stepping_back_still_sorts_later(
    monkeypatch: pytest.MonkeyPatch, frozen_ids: int
) -> None:
    first = new_ulid()
    monkeypatch.setattr(ids, "_millis", lambda: frozen_ids - 5)
    assert new_ulid() > first


@pytest.mark.usefixtures("frozen_ids")
def test_threads_mint_unique_ids_each_in_order() -> None:
    per_thread: list[list[str]] = [[] for _ in range(8)]

    def mint(out: list[str]) -> None:
        out.extend(new_ulid() for _ in range(2_000))

    threads = [threading.Thread(target=mint, args=(out,)) for out in per_thread]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    every = [i for out in per_thread for i in out]
    assert len(set(every)) == len(every) == 16_000
    assert all(out == sorted(out) for out in per_thread)


@pytest.mark.skipif(not hasattr(os, "fork"), reason="needs os.fork")
@pytest.mark.filterwarnings("ignore:This process .* is multi-threaded:DeprecationWarning")
@pytest.mark.usefixtures("frozen_ids")
def test_a_forked_child_does_not_mint_its_parents_next_id() -> None:
    new_ulid()
    read, write = os.pipe()
    pid = os.fork()
    if pid == 0:  # the child: one id, back through the pipe
        os.close(read)
        os.write(write, new_ulid().encode())
        os._exit(0)
    os.close(write)
    child = os.read(read, 64).decode()
    os.close(read)
    os.waitpid(pid, 0)
    assert re.fullmatch(ULID_PATTERN, child)
    assert child != new_ulid()


def test_random_ulid_is_not_a_successor(frozen_ids: int) -> None:
    """The idempotency lease's owner token: not the successor of an id minted just before."""
    new_ulid()
    tokens = [random_ulid() for _ in range(200)]
    assert len(set(tokens)) == 200
    assert all(re.fullmatch(ULID_PATTERN, t) for t in tokens)
    assert new_ulid() not in tokens
    assert tokens != sorted(tokens)
    assert {t[:10] for t in tokens} == {ids._encode(frozen_ids << 80)[:10]}  # random bits only


def test_ulid_after_still_sorts_after_a_foreign_id() -> None:
    ahead = ids._encode((ids._millis() + 60_000) << 80)
    assert ids.ulid_after(ahead) > ahead
