"""No shipped prompt quotes a labelled eval case (#1001; NFR-QUAL-3, NFR-QUAL-4).

A prompt that quotes an eval case word for word answers that case, so the eval's score is
partly measured on items the prompt gives away (#748's salience example and #747's
consolidation example did). This guard keeps the two disjoint.

**What is compared.** Every string value of every record in every ``evals/sets/**/*.jsonl``,
except the ``id``, ``family`` and ``origin`` keys (labels, not text a model reads), against:

- every shipped template, ``src/epistrel/orchestrator/models/templates/*.j2``, with its Jinja
  tags (``{# #}``, ``{% %}``, ``{{ }}``) stripped; and
- the Python-held prompt strings: every string inside an upper-case module-level constant of
  the model-call snapshot's guarded modules (``tests.model_call_support.guarded_modules()``);
  and
- the string literals inside every function the snapshot hashes by source
  (``tests.model_call_support.SOURCES``, #1084): each ``str`` constant in the function's
  ``ast``, the literal parts of an f-string included and its docstring left out. The loop reads
  ``SOURCES`` itself, so an entry added there is scanned without editing this file.

Both sides are lower-cased, apostrophes dropped, other punctuation turned into spaces and
whitespace collapsed. A case string matches when one of its 8-word shingles appears in a
prompt. A case string of 5 to 7 words has no 8-word shingle, so the whole string is its one
shingle (the consolidation episodes are 5 to 7 words each: "sleeps in the chair facing the
hall" is 7). Shorter strings are not compared: 4 words or fewer are stock phrases ("the front
door"). The shingle length stays at 8: it found no stock-phrase false positive at #1001.

**Not covered:** text a consumer supplies at run time (a story's own prose, a stored policy),
and paraphrases or overlaps shorter than the shingle. Both globs are asserted non-empty, so a
moved directory fails rather than passing vacuously; a new set or template is covered without
editing this file.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
import sys
import textwrap
from collections.abc import Iterator, Mapping
from typing import Any

import pytest

from epistrel.tools.generated import REPO_ROOT
from tests import model_call_support

SETS = REPO_ROOT / "evals" / "sets"
TEMPLATES = REPO_ROOT / "src" / "epistrel" / "orchestrator" / "models" / "templates"
SHINGLE = 8
MIN_WHOLE = 5
LABEL_KEYS = frozenset({"id", "family", "origin"})

_JINJA = re.compile(r"\{#.*?#\}|\{%.*?%\}|\{\{.*?\}\}", re.DOTALL)
_APOSTROPHE = re.compile("['\\u2019]")
_PUNCTUATION = re.compile(r"[^\w\s]|_")


def normalise(text: str) -> list[str]:
    """``text`` as lower-case words, apostrophes dropped and other punctuation removed."""
    text = _APOSTROPHE.sub("", text.lower())
    return _PUNCTUATION.sub(" ", text).split()


def shingles(words: list[str]) -> set[str]:
    """Each 8-word run of ``words``; a 5- to 7-word string is its own one shingle."""
    if len(words) < SHINGLE:
        return {" ".join(words)} if len(words) >= MIN_WHOLE else set()
    return {" ".join(words[i : i + SHINGLE]) for i in range(len(words) - SHINGLE + 1)}


def _strings(value: Any, *, skip_labels: bool) -> Iterator[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for key, item in value.items():
            if not (skip_labels and key in LABEL_KEYS):
                yield from _strings(item, skip_labels=skip_labels)
    elif isinstance(value, (list, tuple, set, frozenset)):
        for item in value:
            yield from _strings(item, skip_labels=skip_labels)


def case_shingles() -> dict[str, tuple[str, str]]:
    """Each labelled shingle and the first (set file, case id) it comes from."""
    found: dict[str, tuple[str, str]] = {}
    files = sorted(SETS.rglob("*.jsonl"))
    assert files, f"no labelled sets under {SETS}"
    for path in files:
        where = str(path.relative_to(REPO_ROOT))
        for number, line in enumerate(path.read_text("utf-8").splitlines(), start=1):
            if not line.strip():
                continue
            record = json.loads(line)
            case = str(record.get("id", f"line {number}")) if isinstance(record, dict) else ""
            for text in _strings(record, skip_labels=True):
                for shingle in shingles(normalise(text)):
                    found.setdefault(shingle, (where, case or f"line {number}"))
    return found


def source_strings(dotted: str) -> list[str]:
    """#1084: the string constants of ``SOURCES`` entry ``dotted``'s source (f-string literal
    parts included), in source order, without its docstring."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(model_call_support.resolve(dotted))))
    docstrings: set[int] = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.body:
            first = node.body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    found = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant)
        and isinstance(node.value, str)
        and id(node) not in docstrings
    ]
    return [str(n.value) for n in sorted(found, key=lambda n: (n.lineno, n.col_offset))]


def prompts() -> dict[str, str]:
    """Each shipped prompt's normalised text, by where it lives."""
    found: dict[str, str] = {}
    templates = sorted(TEMPLATES.glob("*.j2"))
    assert templates, f"no templates under {TEMPLATES}"
    for path in templates:
        text = _JINJA.sub(" ", path.read_text("utf-8"))
        found[str(path.relative_to(REPO_ROOT))] = " ".join(normalise(text))
    for module in model_call_support.guarded_modules():
        loaded = importlib.import_module(module)
        for name in vars(loaded):
            if not name.isupper():
                continue
            for index, text in enumerate(_strings(getattr(loaded, name), skip_labels=False)):
                found[f"{module}.{name}[{index}]"] = " ".join(normalise(text))
    for dotted in model_call_support.SOURCES:
        for index, text in enumerate(source_strings(dotted)):
            found[f"source:{dotted}[{index}]"] = " ".join(normalise(text))
    return found


def overlaps() -> list[str]:
    """Each labelled shingle found in a prompt: shingle, prompt, set and case id."""
    labelled = case_shingles()
    found: list[str] = []
    for where, text in prompts().items():
        padded = f" {text} "
        for shingle, (set_file, case) in labelled.items():
            if f" {shingle} " in padded:
                found.append(f"{shingle!r} in {where} (from {set_file}, case {case})")
    return sorted(found)


def test_no_shipped_prompt_quotes_a_labelled_eval_case() -> None:
    found = overlaps()
    assert found == [], "a shipped prompt quotes eval cases:\n" + "\n".join(found)


def test_the_shingles_catch_a_quote_and_ignore_a_stock_phrase() -> None:
    quote = "Sunday dinner is at Aunt Ruth's this week."
    assert shingles(normalise(quote)) == {"sunday dinner is at aunt ruths this week"}
    assert shingles(normalise("sleeps in the chair facing the hall")) == {
        "sleeps in the chair facing the hall"
    }
    assert shingles(normalise("the front door")) == set()
    assert normalise(_JINJA.sub(" ", "a {{ x }} b {% if y %}c{% endif %}")) == ["a", "b", "c"]


def test_both_sides_are_found() -> None:
    assert len(case_shingles()) > 1000
    found = prompts()
    assert any(key.endswith("salience.j2") for key in found)
    assert any(not key.endswith(".j2") for key in found)
    constraint = "source:epistrel.orchestrator.repair.ladder.RepairLadder._constraint"
    assert any(key.startswith(constraint) for key in found)
    assert {k.split("[")[0] for k in found if k.startswith("source:")} <= {
        f"source:{d}" for d in model_call_support.SOURCES
    }


PLANTED = "Linda, do you know what Frank keeps locked in his desk drawer?"
"""#1084's repro sentence, the text of labelled case ``asymmetry-family-0001``."""


def test_a_quote_inside_a_source_function_is_found(monkeypatch: pytest.MonkeyPatch) -> None:
    """#1084: an eval line inside a ``SOURCES`` function's f-string (``_constraint``'s) is
    reported, naming the function and the case. The docstring is not read."""
    dotted = "epistrel.orchestrator.repair.ladder.RepairLadder._constraint"
    shipped = source_strings
    documented = 0
    for name in model_call_support.SOURCES:
        docstring = model_call_support.resolve(name).__doc__
        if docstring:
            documented += 1
            assert docstring not in shipped(name), name
    assert documented

    def planted(name: str) -> list[str]:
        strings = shipped(name)
        return [*strings, f"{PLANTED} Do not claim to remember "] if name == dotted else strings

    monkeypatch.setattr(sys.modules[__name__], "source_strings", planted)
    found = overlaps()
    assert found, "the planted quote was not found"
    assert all(f"source:{dotted}[" in f for f in found), found
    assert any("case asymmetry-family-0001" in f for f in found), found
