"""E-06's miss tally (#939): where generation conformance (NFR-QUAL-7) is lost, from a run's
results journal and, when given, its per-call trace (#737).

``uv run python tools/e06_tally.py <journal.jsonl> [--calls <run.calls.jsonl.gz>]`` prints a
Markdown tally:

- **by part:** of the E-06 case-seed outcomes, how many miss conformance, structure, length
  and (repeat cases) repeatability;
- **structure, by code:** each structure miss's stable code (``structure_code``): a model
  error's ``model_error:<code>:<reason>``, else the line's ``detail.structure_code``
  (``e06_conformance.structure_code``; derived the same way for a line written before it),
  with its share of all outcomes. A code at or above ``THRESHOLD`` (0.5 %) needs a fix;
- **first-attempt refusals, by shape** (with ``--calls``): every retried render call
  (``generation`` and ``regeneration`` purposes) is a refused earlier attempt, and its
  correction message names the refusal. The shape is the correction's problem with numbers
  and parenthesised details folded (``shape``), counted per attempt and as a share of the
  outcomes.

It reads ids, codes and the engine's own correction text, never model output, so the tally is
safe to post.
"""

from __future__ import annotations

import argparse
import gzip
import json
import re
import sys
from collections import Counter
from collections.abc import Iterable, Mapping
from pathlib import Path
from typing import Any

SUITE = "e06"
THRESHOLD = 0.005
RENDER_PURPOSES = frozenset({"generation", "regeneration"})
CONFORMANCE = "nfr_qual_7.conformance"
PARTS = (
    ("conformance", CONFORMANCE),
    ("structure", "e06.structure_valid"),
    ("length", "e06.length_honoured"),
    ("repeat", "e06.repeatable"),
)
_PREFIX = "Your previous answer could not be used: "
_SUFFIX = re.compile(r"\.\s*Reply again with only the JSON object, and nothing else\.\s*$")
_VALUE_ERROR = re.compile(r"^the object: Value error, ")


def code_of(line: Mapping[str, Any]) -> str | None:
    """A journal line's structure-miss code (module docstring); ``None``: no miss."""
    detail = line.get("detail") or {}
    if "model_error" in detail:
        return f"model_error:{detail['model_error']}:{detail.get('reason', '')}"
    if line.get("observations", {}).get("e06.structure_valid") is not False:
        return None
    if detail.get("structure_code"):
        return str(detail["structure_code"])
    causes = detail.get("fallback_causes") or []
    if causes:
        return f"fallback:{causes[0]}"
    problems = detail.get("problems") or []
    return str(problems[0]) if problems else "unknown"


def shape(correction: str) -> str:
    """A correction message's refusal, folded to a stable shape (module docstring)."""
    text = correction.removeprefix(_PREFIX)
    text = _SUFFIX.sub("", text)
    text = _VALUE_ERROR.sub("", text)
    text = re.sub(r"\([^)]*\)", "(…)", text)
    text = re.sub(r"\d+", "N", text)
    return text.strip()[:140]


def read_jsonl(path: Path) -> Iterable[dict[str, Any]]:
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as handle:
        for raw in handle:
            if raw.strip():
                try:
                    yield json.loads(raw)
                except json.JSONDecodeError:
                    continue  # a torn last line (the journal's durability note)


def tally(lines: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    outcomes = [line for line in lines if line.get("suite") == SUITE]
    parts: dict[str, tuple[int, int]] = {}
    for name, metric in PARTS:
        seen = [
            line for line in outcomes if (line.get("observations") or {}).get(metric) is not None
        ]
        missed = sum(1 for line in seen if (line.get("observations") or {}).get(metric) is False)
        parts[name] = (missed, len(seen))
    codes = Counter(code for line in outcomes if (code := code_of(line)) is not None)
    return {"outcomes": len(outcomes), "parts": parts, "codes": codes}


def refusals(calls: Iterable[Mapping[str, Any]]) -> Counter[tuple[str, str]]:
    """``(schema, shape)`` → retried render calls whose correction names it."""
    found: Counter[tuple[str, str]] = Counter()
    for call in calls:
        if call.get("suite") != SUITE or call.get("purpose") not in RENDER_PURPOSES:
            continue
        if int(call.get("attempt") or 1) < 2:
            continue
        messages = call.get("messages") or []
        last = messages[-1] if messages else {}
        content = last.get("content") if isinstance(last, dict) else None
        if isinstance(content, str) and content.startswith(_PREFIX):
            found[(str(call.get("schema")), shape(content))] += 1
    return found


def _pct(n: int, total: int) -> str:
    return f"{100 * n / total:.2f}%" if total else "—"


def render(result: Mapping[str, Any], shapes: Counter[tuple[str, str]] | None) -> str:
    total = result["outcomes"]
    out = [f"**E-06 tally** over {total} case-seed outcomes (fix threshold {THRESHOLD:.1%}).", ""]
    out += ["| Part | Misses | Of | Share |", "|---|---|---|---|"]
    for name, (missed, seen) in result["parts"].items():
        out.append(f"| {name} | {missed} | {seen} | {_pct(missed, seen)} |")
    out += [
        "",
        "| Structure code | Misses | Share of outcomes | ≥ threshold |",
        "|---|---|---|---|",
    ]
    for code, n in result["codes"].most_common():
        flag = "yes" if total and n / total >= THRESHOLD else "no"
        out.append(f"| `{code}` | {n} | {_pct(n, total)} | {flag} |")
    if shapes is not None:
        out += [
            "",
            "| Render | Refusal shape (from the retry's correction) | Attempts | Per outcome |",
            "|---|---|---|---|",
        ]
        for (schema, refused), n in shapes.most_common():
            out.append(f"| `{schema}` | {refused} | {n} | {_pct(n, total)} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("journal", type=Path, help="the run's results journal (.jsonl)")
    parser.add_argument("--calls", type=Path, help="the run's call trace (.calls.jsonl.gz)")
    args = parser.parse_args(argv)
    result = tally(read_jsonl(args.journal))
    shapes = refusals(read_jsonl(args.calls)) if args.calls else None
    sys.stdout.write(render(result, shapes) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
