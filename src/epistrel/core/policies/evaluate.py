"""The deterministic policy evaluator (FR-GUARD-2,9): ``evaluate(text, policies, target)``.

A pure function of its arguments. It reads no database and no story memory, so a verdict
depends only on the text and the policies (FR-GUARD-2,3). It checks every policy that applies
to ``target`` and is deterministic, and returns one ``Violation`` per policy broken, in the
order the policies were given. Model-assisted policies are skipped silently; #56/#68 evaluate
them through the ``guardrail check`` purpose.

- ``wordlist`` and ``entity_list``: the text and each entry are normalised (NFKC, case folded)
  and split into words. A word is a run of the erasure matcher's word characters
  (``core.erasure.matcher.WORD_CHAR``: ``\\w`` and every combining mark of a script, so a
  Devanagari vowel sign is inside its word: #417), and an apostrophe inside a word joins it.
  An entry matches when its words appear consecutively in the text, so "kill" does not match
  "skill", "don" does not match "don't" and "राम" does not match "रामायण".
- ``regex``: ``re.search`` with ``IGNORECASE``. Each policy's patterns run in a child process
  (``regex_worker``) under a deadline (``REGEX_DEADLINE_S``). ``re`` holds the interpreter's
  lock for a whole match and nothing can interrupt it inside one interpreter, so catastrophic
  backtracking is contained by a process the caller can kill. The deadline starts once a worker
  is ready. A search that misses it, or that waits longer than ``STARTUP_GRACE_S`` for a worker,
  counts as a **violation** (fail closed), and its worker is killed and reaped, never left
  running (#290). At most ``REGEX_WORKERS`` workers exist; idle ones are reused.
"""

from __future__ import annotations

import atexit
import json
import os
import re
import selectors
import subprocess
import sys
import threading
import time
import unicodedata
from collections.abc import Iterable
from pathlib import Path

from epistrel.core.erasure.matcher import WORD_CHAR
from epistrel.core.policies import regex_worker
from epistrel.core.policies.model import (
    EntityListRule,
    ModelAssistedRule,
    Policy,
    RegexRule,
    Target,
    Violation,
    WordlistRule,
)

REGEX_DEADLINE_S = 0.05
REGEX_WORKERS = 4
STARTUP_GRACE_S = 5.0
"""How long a search may wait for a worker before it counts as missing its deadline."""

_WORD = re.compile(rf"{WORD_CHAR}+(?:['\u2019]{WORD_CHAR}+)*")
"""A policy word: a run of the erasure matcher's word characters (``WORD_CHAR``, #417), an
apostrophe inside it joining it."""


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


class _RegexWorker:
    """One child process running ``regex_worker``; killed when a search misses its deadline."""

    def __init__(self) -> None:
        self.process = subprocess.Popen(  # noqa: S603 - fixed argv, no user input
            [sys.executable, "-I", str(Path(regex_worker.__file__))],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            bufsize=0,
        )
        if self.process.stdin is None or self.process.stdout is None:  # pragma: no cover
            raise RuntimeError("regex worker started without pipes")
        self._stdin = self.process.stdin
        self._stdout = self.process.stdout
        self._buffer = b""

    def _readline(self, until: float) -> bytes | None:
        """The next line from the worker, or ``None`` when ``until`` passed or it closed."""
        fd = self._stdout.fileno()
        with selectors.DefaultSelector() as selector:
            selector.register(fd, selectors.EVENT_READ)
            while b"\n" not in self._buffer:
                remaining = until - time.monotonic()
                if remaining <= 0 or not selector.select(remaining):
                    return None
                chunk = os.read(fd, 4096)
                if not chunk:
                    return None
                self._buffer += chunk
        line, _, self._buffer = self._buffer.partition(b"\n")
        return line

    def ready(self, grace: float) -> bool:
        return self._readline(time.monotonic() + grace) == b"ready"

    def search(self, patterns: tuple[str, ...], text: str, deadline: float) -> bool | None:
        """Whether any pattern matches; ``None`` when the deadline passed first."""
        request = json.dumps({"p": list(patterns), "t": text}).encode() + b"\n"
        try:
            self._stdin.write(request)
        except OSError:
            return None
        answer = self._readline(time.monotonic() + deadline)
        return None if answer not in (b"0", b"1") else answer == b"1"

    def kill(self) -> None:
        self.process.kill()
        self.process.wait()
        self._stdin.close()
        self._stdout.close()


class _RegexPool:
    """Up to ``REGEX_WORKERS`` worker processes, started lazily and reused while they answer in
    time; a worker that misses a deadline (or fails) is killed and replaced on demand."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(REGEX_WORKERS)
        self._idle: list[_RegexWorker] = []
        self._live: list[_RegexWorker] = []

    def processes(self) -> list[subprocess.Popen[bytes]]:
        """The worker processes still running (for tests and diagnostics)."""
        with self._lock:
            return [w.process for w in self._live if w.process.poll() is None]

    def _take(self) -> _RegexWorker | None:
        with self._lock:
            while self._idle:
                worker = self._idle.pop()
                if worker.process.poll() is None:
                    return worker
                self._drop(worker)
        worker = _RegexWorker()
        with self._lock:
            self._live.append(worker)
        if worker.ready(STARTUP_GRACE_S):
            return worker
        self._discard(worker)
        return None

    def _drop(self, worker: _RegexWorker) -> None:
        if worker in self._live:
            self._live.remove(worker)

    def _discard(self, worker: _RegexWorker) -> None:
        worker.kill()
        with self._lock:
            self._drop(worker)

    def search(self, patterns: tuple[str, ...], text: str, deadline: float) -> bool | None:
        """Whether any pattern matches; ``None`` when the deadline passed first."""
        if not self._slots.acquire(timeout=STARTUP_GRACE_S):
            return None
        try:
            worker = self._take()
            if worker is None:
                return None
            matched = worker.search(patterns, text, deadline)
            if matched is None:
                self._discard(worker)
            else:
                with self._lock:
                    self._idle.append(worker)
            return matched
        finally:
            self._slots.release()

    def close(self) -> None:
        """Kill every worker (at interpreter exit)."""
        with self._lock:
            workers, self._live, self._idle = self._live, [], []
        for worker in workers:
            worker.kill()


_REGEX = _RegexPool()
atexit.register(_REGEX.close)


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


def matched_span(text: str, policy: Policy) -> str | None:
    """The part of ``text`` a ``wordlist`` or ``entity_list`` policy matches, as it stands in the
    text (its first match, by the policy's own entry order); ``None`` for another rule, or when
    the words cannot be located again in the original text. A report quotes it (#111): the
    verdict is ``evaluate``'s, never this function's."""
    rule = policy.rule
    if isinstance(rule, WordlistRule):
        entries: tuple[str, ...] = rule.words
    elif isinstance(rule, EntityListRule):
        entries = rule.names
    else:
        return None
    tokens = [(match.start(), match.end(), words(match.group(0))) for match in _WORD.finditer(text)]
    flat: list[tuple[int, int, str]] = [
        (start, end, word) for start, end, found in tokens for word in found
    ]
    for entry in entries:
        phrase = words(entry)
        size = len(phrase)
        for index in range(len(flat) - size + 1):
            if size and tuple(word for _, _, word in flat[index : index + size]) == phrase:
                return text[flat[index][0] : flat[index + size - 1][1]]
    return None


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
