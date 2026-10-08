"""check-lexicon: a part may use only capability terms its milestone has delivered (docs/04 §13)."""

from __future__ import annotations

import ast
import json
import re
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from tools.gates.spec import ID_PATTERN, SpecError, milestone_label, milestone_number

LEXICON_MARKER = "**Capability lexicon (CI).**"
LEXICON_HEADER = "| Milestone | Capability terms |"
NOT_COUNTED = "Not counted"
WORD_RE = re.compile(r"[A-Z]+(?![a-z])|[A-Z][a-z]+|[a-z]+")
STRIP_RES = (
    re.compile(rf"{ID_PATTERN}(?:\.\.\d+)?"),  # requirement IDs and ranges
    re.compile(r"\b[A-Z]-[A-Z0-9][A-Z0-9-]*\b"),  # docs/04 test IDs: I-10, S-ERASURE-BOUNDARY
    re.compile(r"\(M\d+\)"),  # (Mk) annotations
)
IRREGULAR_FORMS = {
    "rewrite": ("rewritten", "rewrote"),
    "forget": ("forgotten", "forgot"),
    "think": ("thought",),
    "perceive": ("perceived",),
}


class LexiconError(Exception):
    """The lexicon table or parts file cannot be interpreted (structural, exit 2)."""


@dataclass(frozen=True)
class Term:
    text: str
    milestone: int

    @property
    def words(self) -> list[str]:
        return [w for w in re.split(r"[\s\-]+", self.text.strip("`")) if w]

    @property
    def capitalized(self) -> bool:
        first = self.words[0]
        return first[0].isupper() and not first.isupper()

    @property
    def acronym(self) -> bool:
        return self.words[0].isupper() and len(self.words[0]) > 1


@dataclass(frozen=True)
class Lexicon:
    terms: list[Term]
    not_counted: list[str]


@dataclass(frozen=True)
class Finding:
    node_id: str
    surface: str
    term: str
    term_milestone: int
    part_milestone: int

    def line(self) -> str:
        theirs, ours = milestone_label(self.term_milestone), milestone_label(self.part_milestone)
        return f'{self.node_id} uses "{self.surface}" ({self.term}, {theirs}) in an {ours} part'


# --- table -----------------------------------------------------------------------------------


def parse_lexicon(docs_text: str, valid_milestones: set[int]) -> Lexicon:
    lines = docs_text.splitlines()
    starts = [i for i, line in enumerate(lines) if line.startswith(LEXICON_MARKER)]
    if len(starts) != 1:
        raise LexiconError(
            f"expected one {LEXICON_MARKER!r} paragraph in docs/04, found {len(starts)}"
        )
    headers = [i for i in range(starts[0], len(lines)) if lines[i].strip() == LEXICON_HEADER]
    if not headers:
        raise LexiconError("lexicon table header not found after the lexicon paragraph")
    terms: list[Term] = []
    not_counted: list[str] = []
    owner: dict[str, int] = {}
    for line in lines[headers[0] + 2 :]:
        if not line.lstrip().startswith("|"):
            break
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) != 2:
            raise LexiconError(f"lexicon row does not have two cells: {line.strip()!r}")
        entries = [t.strip().strip("`") for t in cells[1].split(";") if t.strip()]
        if cells[0] == NOT_COUNTED:
            not_counted.extend(entries)
            continue
        try:
            milestone = milestone_number(cells[0].replace("**", ""))
        except SpecError as exc:
            raise LexiconError(f"lexicon row: {exc}") from exc
        if milestone not in valid_milestones:
            raise LexiconError(f"lexicon row {cells[0]} is not a §22 milestone")
        for entry in entries:
            key = entry.lower()
            if key in owner and owner[key] != milestone:
                raise LexiconError(f"term {entry!r} appears under two milestones")
            owner[key] = milestone
            terms.append(Term(entry, milestone))
    if not terms:
        raise LexiconError("lexicon table has no milestone rows")
    return Lexicon(terms, not_counted)


# --- matching --------------------------------------------------------------------------------


def inflections(word: str) -> set[str]:
    """Forms a lexicon word may take in test text (lower-case): plural, -ed, -ing and friends."""
    w = word.lower()
    forms = {w, w + "s", w + "es", w + "ed", w + "ing"}
    if w.endswith("e"):
        forms |= {w[:-1] + "ing", w + "d"}
    if w.endswith("y") and len(w) > 1 and w[-2] not in "aeiou":
        forms |= {w[:-1] + "ies", w[:-1] + "ied"}
    if len(w) > 2 and w[-1] not in "aeiouy" and w[-2] in "aeiou" and w[-3] not in "aeiou":
        forms |= {w + w[-1] + "ing", w + w[-1] + "ed"}
    forms |= set(IRREGULAR_FORMS.get(w, ()))
    return forms


def tokenize(text: str) -> list[str]:
    """Words only: `_`, `-`, whitespace and punctuation separate; CamelCase splits; digits drop."""
    return WORD_RE.findall(text)


def _word_matches(token: str, word: str, *, last: bool, exact_case: bool) -> bool:
    if word.endswith("*") and last:
        stem = word[:-1]
        return token.lower().startswith(stem.lower()) if not exact_case else token.startswith(stem)
    candidates = inflections(word) if last else {word.lower()}
    if exact_case:
        # first letter must match the term's case; the rest compares in lower case
        return token[0] == word[0] and token.lower() in candidates
    return token.lower() in candidates


def _acronym_matches(token: str, word: str) -> bool:
    return token == word


def find_phrase(
    tokens: list[str], words: list[str], *, exact_case: bool, acronym: bool
) -> list[int]:
    """Start indexes where the phrase occurs."""
    hits: list[int] = []
    n = len(words)
    for i in range(len(tokens) - n + 1):
        ok = True
        for j, word in enumerate(words):
            token = tokens[i + j]
            last = j == n - 1
            if acronym and j == 0:
                ok = _acronym_matches(token, word)
            else:
                ok = _word_matches(token, word, last=last, exact_case=exact_case and j == 0)
            if not ok:
                break
        if ok:
            hits.append(i)
    return hits


def body_text(source: str) -> str:
    """The function body only: decorators and the `def` signature are dropped; IDs stripped."""
    text = source
    try:
        module = ast.parse(textwrap.dedent(source))
        fn = next(
            (n for n in module.body if isinstance(n, ast.FunctionDef | ast.AsyncFunctionDef)), None
        )
        if fn is not None and fn.body:
            lines = textwrap.dedent(source).splitlines()
            text = "\n".join(lines[fn.body[0].lineno - 1 :])
    except SyntaxError:
        pass
    for pattern in STRIP_RES:
        text = pattern.sub(" ", text)
    return text


def remove_not_counted(tokens: list[str], phrases: list[str]) -> list[str]:
    for phrase in phrases:
        words = [w for w in re.split(r"[\s\-]+", phrase) if w]
        while True:
            hits = find_phrase(tokens, words, exact_case=False, acronym=False)
            if not hits:
                break
            start = hits[0]
            tokens = tokens[:start] + tokens[start + len(words) :]
    return tokens


def check(parts: list[dict[str, Any]], lexicon: Lexicon) -> list[Finding]:
    """One finding per (test, term): a later milestone's term used in a part's body text."""
    grouped: dict[str, tuple[int, str]] = {}
    for entry in parts:
        label = entry.get("milestone")
        if not label:
            continue
        milestone = milestone_number(str(label))
        node_id = str(entry["node_id"])
        current = grouped.get(node_id)
        if current is None or milestone > current[0]:
            grouped[node_id] = (milestone, str(entry.get("source", "")))
    findings: list[Finding] = []
    for node_id, (part_milestone, source) in sorted(grouped.items()):
        tokens = remove_not_counted(tokenize(body_text(source)), lexicon.not_counted)
        for term in lexicon.terms:
            if term.milestone <= part_milestone:
                continue
            hits = find_phrase(
                tokens, term.words, exact_case=term.capitalized, acronym=term.acronym
            )
            if hits:
                surface = " ".join(tokens[hits[0] : hits[0] + len(term.words)])
                findings.append(
                    Finding(node_id, surface, term.text, term.milestone, part_milestone)
                )
    return findings


def load_parts(path: Path) -> list[dict[str, Any]]:
    if not path.exists():
        raise LexiconError(
            f"{path} not found — run `python -m tools.gates check-parts --json` first"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LexiconError(f"cannot read {path}: {exc}") from exc
    if not isinstance(data, list):
        raise LexiconError(f"{path}: expected a JSON list of parts")
    return [d for d in data if isinstance(d, dict)]
