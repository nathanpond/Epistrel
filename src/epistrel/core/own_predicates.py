"""The predicates an operation auto-registered itself (FR-CONC-16).

A structured turn registers an unregistered relation in a short transaction of its own while it
generates (``core.propositions.registry.auto_register``). Its commit compares the story's
predicate registry with the one it captured at start (``core.config_dependencies``), so without
this it would count its own registration as an interpretive change made under it and re-check
itself (P-08's structured row: a turn conflicted when its re-check failed and its budget could
not pay to generate again). ``run_with_revalidation`` records the turn's registrations into the
operation's ``own_predicates`` (``recording``); its configuration captures leave those rows
out. Another operation's registration still changes the hash and re-checks the turn.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_OWN: ContextVar[set[str] | None] = ContextVar("epistrel_own_predicates", default=None)


@contextmanager
def recording(into: set[str]) -> Iterator[None]:
    """Within the block, every predicate ``auto_register`` creates is added to ``into``."""
    token = _OWN.set(into)
    try:
        yield
    finally:
        _OWN.reset(token)


def note(predicate_id: str) -> None:
    """``predicate_id`` was registered by the operation recording here, if one is."""
    own = _OWN.get()
    if own is not None:
        own.add(predicate_id)


__all__ = ["note", "recording"]
