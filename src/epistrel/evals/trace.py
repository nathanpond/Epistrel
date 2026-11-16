"""The per-call trace of an eval run (#737): every model call's request and response, kept
next to the results journal so a failed call can be read from a file.

**What a record is.** One JSON object per HTTP request a model call made (a call the adapter
retried with the legacy ``max_tokens`` or without sampling made two), version ``v: 1``:

- the journal key of the case it belongs to (``suite``, ``suite_fingerprint``, ``case``,
  ``digest``, ``seed``) with the run's ``fingerprint``, ``preset`` and ``model``, the case's call
  index ``call`` and the request within the call ``http``; a call outside a case has
  ``phase`` ``calibration`` or ``setup`` and a null case (``phase`` is ``case`` otherwise);
- ``purpose``, ``attempt`` (as ``send`` counts it), ``schema``, the ``structured_mode`` sent
  (``json_schema_strict``, ``json_schema``, ``parse_validate_retry`` or null), and ``judge``
  (the harness's own judge calls: E-03's entailment judge and ``quality_judge``);
- ``messages`` in wire form (an image's ``data:`` URL elided to its digest and length) and
  ``params`` as sent (``temperature``/``top_p``, ``max_completion_tokens`` or ``max_tokens``,
  ``seed``, ``stop``, any provider extras such as ``extra_body``);
- ``response``: ``text``, ``text_len``, ``finish_reason``, ``refusal``, ``tokens_in``,
  ``tokens_out``, ``completion_tokens_details`` when the provider sends one (#735: Anthropic's
  endpoint sends none, so a hidden-reasoning share shows as ``tokens_out`` against
  ``max_completion_tokens`` with a short ``text_len``), ``request_id``, ``reported_version``;
- ``error`` on a failed request: the exception type, HTTP ``status``, the provider's error
  ``body`` and ``request_id``; ``raised``: what the call ended in, as the harness saw it;
- ``latency_ms`` (the request) and ``call_latency_ms`` (the call), ``engine_sha``, ``session``
  (one per process) and ``complete`` (whether the case's journal row was written).

Text fields are capped at 64 KiB (``truncated: true`` marks a cut one). No API key, and no
``Authorization`` or ``x-api-key`` value, is ever written: the SDK sends the key as a header,
which the trace never reads, and every key the run was given is scrubbed from each line as a
second guard (``CallTrace.secrets``).

**Where.** Next to the journal: ``<model>/<preset>-<fp12>.calls.jsonl.gz`` for ``certify``
(``trace_path``), ``<model>/run-<fp12>.calls.jsonl.gz`` for ``run`` and
``<judge>/calibration.calls.jsonl.gz`` for ``calibrate-judge``, under ``.evals-results/``
(ignored by git: the trace holds the model's output). ``--no-trace`` writes nothing.

**Durability and resume.** A case's records are held until the case ends and then appended as
one gzip member, after its journal row (``complete: true``). A case that ends in a harness error
writes its records with ``complete: false``, so the failure stays readable. A case cancelled
mid-play (a second Ctrl-C) writes nothing: it plays again on resume, and its records come
then. A resumed run's journalled cases are not played, so they add no record. A process killed
mid-write leaves a torn last member, which the next open of the file cuts off
(``TraceFile.repair``), so the file always reads as whole records. ``--fresh`` sets the trace
aside with the journal (``.<n>.calls.jsonl.gz``).

The trace is the eval harness's: ``call_trace.CALL_TRACE`` is set only here, there is no setting
for it, and it changes no fingerprint, report or journal row. Read it with any JSONL reader
(``docs/evals/README.md`` has a DuckDB query).
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import uuid
import zlib
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from epistrel.orchestrator.models.call_trace import CALL_TRACE, Exchange

TRACE_VERSION = 1
TEXT_CAP = 64 * 1024
"""Characters a text field keeps (``truncated`` marks a cut one)."""
SUFFIX = ".calls.jsonl.gz"
REDACTED = "[redacted]"
_SETTINGS = frozenset({"model", "messages", "stream", "response_format"})


def _slug(text: str) -> str:
    """A model name as a directory name (``journal._slug``; not imported: the journal imports
    the suites, which import the judge, which imports this module)."""
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("_") or "model"


@dataclass(frozen=True, slots=True)
class CaseKey:
    """The journal key of one played case (``journal.Key``)."""

    suite: str
    suite_fingerprint: str | None
    case: str
    digest: str
    seed: int


@dataclass
class _Case:
    key: CaseKey
    records: list[dict[str, Any]] = field(default_factory=list)
    calls: int = 0


CASE: ContextVar[_Case | None] = ContextVar("TRACE_CASE", default=None)
PHASE: ContextVar[str] = ContextVar("TRACE_PHASE", default="setup")
JUDGING: ContextVar[bool] = ContextVar("TRACE_JUDGING", default=False)


@contextmanager
def judging() -> Iterator[None]:
    """Mark the calls made inside as the harness's judge calls (``judge: true``)."""
    token = JUDGING.set(True)
    try:
        yield
    finally:
        JUDGING.reset(token)


def trace_path(journal_file: Path) -> Path:
    """The trace beside a journal: ``x.jsonl`` → ``x.calls.jsonl.gz``."""
    return journal_file.with_name(journal_file.name.removesuffix(".jsonl") + SUFFIX)


def run_trace_path(root: Path, model: str, fingerprint: str) -> Path:
    """``evals run``'s trace: ``<root>/<model>/run-<fingerprint[:12]>.calls.jsonl.gz``."""
    digest = fingerprint.split(":", 1)[-1][:12]
    return root / _slug(model) / f"run-{digest}{SUFFIX}"


def calibration_trace_path(root: Path, judge: str) -> Path:
    """``calibrate-judge``'s trace: ``<root>/<judge>/calibration.calls.jsonl.gz``."""
    return root / _slug(judge) / f"calibration{SUFFIX}"


def _capped(text: str) -> tuple[str, bool]:
    if len(text) <= TEXT_CAP:
        return text, False
    return text[:TEXT_CAP], True


def _elided(url: str) -> Any:
    if not url.startswith("data:"):
        return url
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:16]
    return {"elided": "data_url", "sha256": digest, "length": len(url)}


def _wire_messages(messages: Any) -> tuple[list[Any], bool]:
    """Messages as sent, ``data:`` URLs elided and text capped; whether any text was cut."""
    cut = False
    shown: list[Any] = []
    for message in messages if isinstance(messages, list) else []:
        if not isinstance(message, Mapping):
            continue
        entry = dict(message)
        content = entry.get("content")
        if isinstance(content, str):
            entry["content"], was = _capped(content)
            cut = cut or was
        elif isinstance(content, list):
            parts: list[Any] = []
            for part in content:
                if isinstance(part, Mapping) and part.get("type") == "image_url":
                    image = dict(part.get("image_url") or {})
                    image["url"] = _elided(str(image.get("url", "")))
                    parts.append({**part, "image_url": image})
                elif isinstance(part, Mapping) and isinstance(part.get("text"), str):
                    text, was = _capped(str(part["text"]))
                    cut = cut or was
                    parts.append({**part, "text": text})
                else:
                    parts.append(part)
            entry["content"] = parts
        shown.append(entry)
    return shown, cut


def structured_mode(body: Mapping[str, Any], schema: str | None) -> str | None:
    """``json_schema_strict``, ``json_schema``, ``parse_validate_retry`` (a schema named but no
    ``response_format`` sent) or ``None`` (an unstructured call)."""
    sent = body.get("response_format")
    if isinstance(sent, Mapping):
        spec = sent.get("json_schema")
        strict = isinstance(spec, Mapping) and spec.get("strict") is True
        return "json_schema_strict" if strict else "json_schema"
    return "parse_validate_retry" if schema else None


class TraceFile:
    """One trace file: whole gzip members appended, a torn last one cut on open."""

    def __init__(self, path: Path, *, fresh: bool = False) -> None:
        self.path = path
        if fresh and path.is_file():
            stem = path.name.removesuffix(SUFFIX)
            number = 1
            while (aside := path.with_name(f"{stem}.{number}{SUFFIX}")).exists():
                number += 1
            path.rename(aside)
        self.repair()
        self.written = 0

    def repair(self) -> int:
        """Cut a torn last gzip member (a process killed mid-write); the bytes cut."""
        if not self.path.is_file():
            return 0
        data = self.path.read_bytes()
        good = offset = 0
        while offset < len(data):
            inflater = zlib.decompressobj(wbits=31)
            try:
                inflater.decompress(data[offset:])
            except zlib.error:
                break
            if not inflater.eof:
                break
            offset = len(data) - len(inflater.unused_data)
            good = offset
        if good == len(data):
            return 0
        with self.path.open("r+b") as handle:
            handle.truncate(good)
        return len(data) - good

    def write(self, lines: Sequence[str]) -> None:
        """Append ``lines`` as one gzip member (nothing for none)."""
        if not lines:
            return
        payload = "".join(line + "\n" for line in lines).encode("utf-8")
        member = gzip.compress(payload, mtime=0)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.path.open("ab") as handle:
            handle.write(member)
            handle.flush()
        self.written += len(lines)


def read(path: Path) -> Iterator[dict[str, Any]]:
    """Every record of a trace file, in order."""
    if not path.is_file():
        return
    with gzip.open(path, "rt", encoding="utf-8") as handle:
        for raw in handle:
            if raw.strip():
                yield json.loads(raw)


class CallTrace:
    """The sink ``ModelAccessLayer.send`` hands each call to during an eval run
    (``call_trace.CallTraceSink``; module docstring)."""

    def __init__(
        self,
        *,
        model: str,
        engine_sha: str,
        file: TraceFile | None = None,
        preset: str | None = None,
        fingerprint: str | None = None,
        secrets: Sequence[str | None] = (),
    ) -> None:
        self.model = model
        self.engine_sha = engine_sha
        self.file = file
        self.preset = preset
        self.fingerprint = fingerprint
        self.secrets = tuple(s for s in secrets if s)
        self.session = uuid.uuid4().hex[:12]
        self.pending: list[dict[str, Any]] = []

    # -- what send hands over ----------------------------------------------------------------

    def record(self, call: dict[str, Any], exchanges: list[Exchange]) -> None:
        case = CASE.get()
        if case is not None:
            case.calls += 1
            index = case.calls
        else:
            index = None
        for number, exchange in enumerate(exchanges, start=1):
            entry = self._entry(call, exchange, case, index, number)
            if case is not None:
                case.records.append(entry)
            else:
                self.pending.append(entry)
        if case is None and self.file is not None:
            self.flush()

    def _entry(
        self,
        call: Mapping[str, Any],
        exchange: Exchange,
        case: _Case | None,
        index: int | None,
        number: int,
    ) -> dict[str, Any]:
        body = exchange.body
        schema = call.get("schema")
        messages, cut = _wire_messages(body.get("messages"))
        response = dict(exchange.response) if exchange.response is not None else None
        if response is not None and isinstance(response.get("text"), str):
            response["text"], was = _capped(response["text"])
            cut = cut or was
        error = dict(exchange.error) if exchange.error is not None else None
        if error is not None and "body" in error:
            shown = json.dumps(error["body"], ensure_ascii=False, default=str)
            if len(shown) > TEXT_CAP:
                error["body"], cut = shown[:TEXT_CAP], True
        key = case.key if case is not None else None
        entry: dict[str, Any] = {
            "v": TRACE_VERSION,
            "session": self.session,
            "phase": "case" if case is not None else PHASE.get(),
            "fingerprint": self.fingerprint,
            "preset": self.preset,
            "model": self.model,
            "suite": key.suite if key else None,
            "suite_fingerprint": key.suite_fingerprint if key else None,
            "case": key.case if key else None,
            "digest": key.digest if key else None,
            "seed": key.seed if key else None,
            "call": index,
            "http": number,
            "purpose": call.get("purpose"),
            "attempt": call.get("attempt"),
            "schema": schema,
            "structured_mode": structured_mode(body, schema),
            "judge": JUDGING.get(),
            "endpoint_model": body.get("model"),
            "messages": messages,
            "params": {k: v for k, v in body.items() if k not in _SETTINGS},
            "response": response,
            "error": error,
            "raised": call.get("raised"),
            "raised_code": call.get("raised_code"),
            "latency_ms": exchange.latency_ms,
            "call_latency_ms": call.get("latency_ms"),
            "engine_sha": self.engine_sha,
        }
        if cut:
            entry["truncated"] = True
        return entry

    # -- cases and phases --------------------------------------------------------------------

    @contextmanager
    def case(self, key: CaseKey) -> Iterator[_Case]:
        """Hold the records of the calls made inside for ``finish``."""
        held = _Case(key)
        token = CASE.set(held)
        try:
            yield held
        finally:
            CASE.reset(token)

    def finish(self, held: _Case, *, complete: bool) -> None:
        """Write a case's held records as one member (module docstring)."""
        if self.file is None:
            self.pending.extend({**r, "complete": complete} for r in held.records)
        else:
            self.file.write([self._line({**r, "complete": complete}) for r in held.records])
        held.records.clear()

    @contextmanager
    def phase(self, name: str) -> Iterator[None]:
        """Calls made inside outside any case are recorded under ``phase: name``."""
        token = PHASE.set(name)
        try:
            yield
        finally:
            PHASE.reset(token)
            if self.file is not None:
                self.flush()

    def attach(
        self, file: TraceFile, *, preset: str | None = None, fingerprint: str | None = None
    ) -> None:
        """Write to ``file`` from now on (certify: one per preset), with what is pending."""
        self.file = file
        self.preset = preset
        self.fingerprint = fingerprint
        for entry in self.pending:
            if entry.get("preset") is None:
                entry["preset"] = preset
            if entry.get("fingerprint") is None:
                entry["fingerprint"] = fingerprint
        self.flush()

    def flush(self) -> None:
        """Write the records held outside any case."""
        if self.file is None or not self.pending:
            return
        pending, self.pending = self.pending, []
        self.file.write([self._line({"complete": True, **r}) for r in pending])

    @contextmanager
    def active(self) -> Iterator[CallTrace]:
        """Turn the trace on for the model calls made inside (``call_trace.CALL_TRACE``)."""
        token = CALL_TRACE.set(self)
        try:
            yield self
        finally:
            CALL_TRACE.reset(token)
            self.flush()

    def _line(self, entry: Mapping[str, Any]) -> str:
        line = json.dumps(entry, sort_keys=True, ensure_ascii=False, default=str)
        for secret in self.secrets:
            line = line.replace(secret, REDACTED)
        return line


__all__ = [
    "SUFFIX",
    "TEXT_CAP",
    "TRACE_VERSION",
    "CallTrace",
    "CaseKey",
    "TraceFile",
    "calibration_trace_path",
    "judging",
    "read",
    "run_trace_path",
    "structured_mode",
    "trace_path",
]
