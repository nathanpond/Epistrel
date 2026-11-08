"""Held model answers: a render made at one call and committed at a later one (#241, #718).

A directed regeneration whose binding conflicts with recorded history is held on a
``directed_resolution`` rewrite session (FR-COMMIT-12). The variant is rendered at the
regenerate call, so the author approves the reply that will commit, and the session holds the
render (``rewrite_sessions.held_render``). The apply plays the regeneration again with the
held answers.

``HeldCalls`` is the part of that hold the Model Access Layer sees:

- **recording**: every completion the play makes is kept, keyed by its purpose and a hash of
  the exact messages sent;
- **replaying**: a completion whose purpose and messages match a kept one answers with it and
  sends nothing. One that does not match is sent to the model as usual (``live``), so a
  prompt the story changed under is rendered again, never answered with a stale render.

Only the answers are kept, never the prompts. The answers are story text, so the column is
indexed and scrubbed like every rewrite session text (``core.erasure.records``).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from typing import Any

from epistrel.orchestrator.models.adapter import Message, ModelResult


def key_of(purpose: str, messages: Sequence[Message]) -> str:
    """The match key of one completion: its purpose and the exact messages (a message's images
    only when it has some, so a text message's key is unchanged), hashed."""
    material = json.dumps(
        [purpose, [[m.role, m.content, *([list(m.images)] if m.images else [])] for m in messages]],
        ensure_ascii=False,
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


class HeldCalls:
    """The completions of one held play: recorded, or replayed from ``recorded``."""

    def __init__(self, recorded: Sequence[dict[str, Any]] | None = None) -> None:
        self.replaying = recorded is not None
        self._kept: list[dict[str, Any]] = []
        self._queues: dict[str, list[ModelResult]] = {}
        for entry in recorded or ():
            self._queues.setdefault(str(entry["key"]), []).append(
                ModelResult.model_validate(entry["result"])
            )
        self.replayed = 0
        self.live = 0

    def take(self, purpose: str, messages: Sequence[Message]) -> ModelResult | None:
        """Replaying: the kept answer to this exact call, consumed in recorded order (``None``:
        none matches, and the call goes to the model)."""
        if not self.replaying:
            return None
        queue = self._queues.get(key_of(purpose, messages))
        if not queue:
            self.live += 1
            return None
        self.replayed += 1
        return queue.pop(0)

    def keep(self, purpose: str, messages: Sequence[Message], result: ModelResult) -> None:
        """Recording: keep an answer the model gave."""
        if self.replaying:
            return
        self._kept.append(
            {
                "key": key_of(purpose, messages),
                "purpose": purpose,
                "result": result.model_dump(mode="json"),
            }
        )

    def document(self) -> list[dict[str, Any]]:
        """What the hold stores: each kept answer with its purpose and match key."""
        return list(self._kept)


HELD_CALLS: ContextVar[HeldCalls | None] = ContextVar("epistrel_held_calls", default=None)
"""The held play under way (``None``: calls go to the model and nothing is kept)."""


@contextmanager
def holding(calls: HeldCalls) -> Iterator[HeldCalls]:
    """Record into, or replay from, ``calls`` for the calls made inside the block."""
    token = HELD_CALLS.set(calls)
    try:
        yield calls
    finally:
        HELD_CALLS.reset(token)


__all__ = ["HELD_CALLS", "HeldCalls", "holding", "key_of"]
