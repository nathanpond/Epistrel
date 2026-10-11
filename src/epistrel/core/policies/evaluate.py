"""The deterministic policy evaluator (FR-GUARD-2,9): ``evaluate(text, policies, target)``.

A pure function of its arguments. It reads no database and no story memory, so a verdict
depends only on the text and the policies (FR-GUARD-2,3). It checks every policy that applies
to ``target`` and is deterministic, and returns one ``Violation`` per policy broken, in the
order the policies were given. Model-assisted policies are skipped silently; #56/#68 evaluate
them through the ``guardrail check`` purpose.

- ``wordlist`` and ``entity_list``: the text and each entry are normalised (NFKC, case folded)
  and split into words. A word is a run of Unicode ``\\w`` characters, and an apostrophe
  inside a word joins it. An entry matches when its words appear consecutively in the text,
  so "kill" does not match "skill" and "don" does not match "don't".
- ``regex``: ``re.search`` with ``IGNORECASE``. Each policy's patterns run in a
  subinterpreter under a deadline (``REGEX_DEADLINE_S``). ``re`` holds its interpreter's lock
  for a whole match, so a thread in this interpreter could not be interrupted, and catastrophic
  backtracking would stall the caller. A subinterpreter has its own lock. The deadline starts
  when a worker picks the search up. A search that misses it, or that waits longer than
  ``STARTUP_GRACE_S`` for a worker, counts as a **violation** (fail closed). Its worker is
  abandoned and the pool is replaced; the abandoned search runs to completion in the
  background.
"""

from __future__ import annotations

import re
import threading
import time
import unicodedata
from collections.abc import Iterable
from concurrent.futures import Future, InterpreterPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout

from epistrel.core.policies.model import (
    EntityListRule,
    ModelAssistedRule,
    Policy,
    RegexRule,
    Target,
    Violation,
    WordlistRule,
)
from epistrel.core.policies.regex_worker import search_any

REGEX_DEADLINE_S = 0.05
REGEX_WORKERS = 4
STARTUP_GRACE_S = 5.0
"""How long a search may wait for a worker before it counts as missing its deadline."""

_WORD = re.compile(r"\w+(?:['\u2019]\w+)*")


def _normalise(text: str) -> str:
    return unicodedata.normalize("NFKC", unicodedata.normalize("NFKC", text).casefold())


def words(text: str) -> tuple[str, ...]:
    """The normalised words of ``text`` (see the module docstring)."""
    return tuple(_WORD.findall(_normalise(text)))


class _Words:
    """The words of one text, with its n-grams built on demand."""

    def __init__(self, text: str) -> None:
        self.words = words(text)
        self._grams: dict[int, frozenset[tuple[str, ...]]] = {}

    def contains(self, entry: str) -> bool:
        phrase = words(entry)
        size = len(phrase)
        if size == 0 or size > len(self.words):
            return False
        grams = self._grams.get(size)
        if grams is None:
            grams = frozenset(self.words[i : i + size] for i in range(len(self.words) - size + 1))
            self._grams[size] = grams
        return phrase in grams


class _RegexPool:
    """A lazily started interpreter pool, replaced whenever a search misses its deadline."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._pool: InterpreterPoolExecutor | None = None

    def search(self, patterns: tuple[str, ...], text: str, deadline: float) -> bool | None:
        """Whether any pattern matches; ``None`` when the deadline passed first."""
        with self._lock:
            if self._pool is None:
                self._pool = InterpreterPoolExecutor(max_workers=REGEX_WORKERS)
            pool = self._pool
        future = pool.submit(search_any, patterns, text)
        try:
            if not _started(future, STARTUP_GRACE_S):
                raise FutureTimeout
            return future.result(timeout=deadline)
        except FutureTimeout:
            future.cancel()
            with self._lock:
                if self._pool is pool:
                    self._pool = None
                    pool.shutdown(wait=False, cancel_futures=True)
            return None


def _started(future: Future[bool], grace: float) -> bool:
    """Wait until a worker has picked the search up. A worker's interpreter is created before
    it takes work, so the deadline measures the search alone, never interpreter start-up."""
    give_up = time.monotonic() + grace
    while not (future.running() or future.done()):
        if time.monotonic() > give_up:
            return False
        time.sleep(0.0005)
    return True


_REGEX = _RegexPool()


def _breaks(policy: Policy, found: _Words, text: str) -> bool:
    rule = policy.rule
    if isinstance(rule, WordlistRule):
        return any(found.contains(word) for word in rule.words)
    if isinstance(rule, EntityListRule):
        return any(found.contains(name) for name in rule.names)
    if isinstance(rule, RegexRule):
        matched = _REGEX.search(rule.patterns, text, REGEX_DEADLINE_S)
        return True if matched is None else matched
    return False  # pragma: no cover - model-assisted rules are filtered out by the caller


def evaluate(text: str, policies: Iterable[Policy], target: Target) -> list[Violation]:
    """The deterministic policies applying to ``target`` that ``text`` breaks, in order."""
    found = _Words(text)
    violations: list[Violation] = []
    seen: set[str] = set()
    for policy in policies:
        if policy.id in seen or target not in policy.applies_to:
            continue
        if isinstance(policy.rule, ModelAssistedRule):
            continue
        seen.add(policy.id)
        if _breaks(policy, found, text):
            violations.append(
                Violation(
                    policy_id=policy.id,
                    name=policy.name,
                    message=policy.message,
                    kind=policy.kind,
                )
            )
    return violations
