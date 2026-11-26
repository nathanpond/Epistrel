"""The guarded set behind ``MODEL_CALL_REVISION`` (#818, owner's R1 (a); ADR-200).

A certification covers what a model call sends. Templates and quality-relevant settings are in
the fingerprint already. This module hashes the rest, the Python-held parts:

- **Constants** (``const:<module>.<NAME>``): every module-level upper-case name assigned in a
  guarded module whose value is plain data (strings, numbers, booleans, ``None``, and
  tuples, lists, sets and mappings of them). The guarded modules are every module under
  ``epistrel.orchestrator`` that imports the Model Access Layer (``MODEL_ACCESS``), found from
  the source so a new caller joins by itself, plus ``EXTRA_MODULES`` (modules that hold prompt
  text or request shape without importing it). Input-truncation caps count. A literal is
  hashed as written; a computed value at run time, except a computed ``dict``, ``list`` or
  ``set``, which may be a registry other imports fill.
- **Named constants elsewhere** (``EXTRA_NAMES``): core's token ceilings that a call sends,
  and core's effect-syntax clock line (#939).
- **Schemas** (``schema:<purpose>``): each delivered purpose's input and response JSON Schema,
  its history field and its variant names. The response schema is sent on the wire, and the
  input schema shapes what the template renders.
- **Functions** (``source:<module>.<name>``): ``inspect.getsource`` of the structured-mode
  choice, the request body builders and every ``*_params`` helper that computes a model call's
  ``ModelParams`` (``SOURCES``; #1003): ``compose.compose.polish_params`` (the polish's token
  ceiling), ``lite.prompt.model_params`` (Lite generation's) and
  ``structured.render.render_params`` (a beat render's). A formula edit in one of them, such
  as dropping ``LENGTH_FACTOR`` from ``polish_params``, changes its hash.

  Found by grepping ``def .*_params`` under ``src/epistrel``. Excluded, because they make no
  model call's params: ``models.config._params``, ``_image_params``, ``_wire_params`` and
  ``check_port_params`` (they parse and validate the deployment's model-config rows, which are
  data); ``models.capability.check_prompt_params`` (a capability probe, not a story call);
  ``images.jobs``' ``params`` property (an image job's stored request, not ``ModelParams``);
  and ``core.retrieval``'s ``position_params`` and ``scope_params`` (SQL bind parameters).

  Not covered: params written inline at a call site rather than in a named function. Their
  literals are invisible here unless they are a module-level plain-data constant (a
  ``ModelParams`` instance is not plain data, so ``verify.verifier.BUILT_IN_PARAMS``'
  ``max_tokens=100`` is not hashed). The inline sites found at #1003: ``directives``,
  ``rewrite.lite``, ``rewrite.apply``, ``rewrite.parse``, ``embeddings.service``,
  ``verify.extraction`` and ``verify.verifier`` (``BUILT_IN_PARAMS``),
  ``images.prompt_builder``, ``models.structured`` (the temperature-0 merge),
  ``structured.render``, ``structured.multi``, ``structured.simultaneous`` and
  ``structured.pipeline``. The probes in ``models.capability`` and ``models.openai_compatible``
  are excluded on purpose.

It does not cover behaviour reached only through data, such as a model-config row, a stored
guardrail policy or a story's settings; those are the deployment's, or in the fingerprint
already. It over-covers rather than under-covers: a constant in a guarded module that no call
sends still needs a bump when it changes.

``uv run python -m tests.model_call_support --write`` rewrites the snapshot. It refuses a
changed set unless ``MODEL_CALL_REVISION`` is already above the snapshot's revision. Entries that
are only added, with none changed or removed, are written at the current revision without a
bump (#1003): widening the guard changes no call. A new constant or function that does change
a call still needs the bump, which is the author's call; the writer names what it added.
"""

from __future__ import annotations

import ast
import hashlib
import importlib
import inspect
import json
import sys
from collections.abc import Mapping
from enum import Enum
from pathlib import Path
from typing import Any

from epistrel.core.profiles.fingerprint import MODEL_CALL_REVISION, canonical_json
from epistrel.tools.generated import REPO_ROOT

SNAPSHOT = REPO_ROOT / "tests" / "fixtures" / "model_call_snapshot.json"
ORCHESTRATOR = REPO_ROOT / "src" / "epistrel" / "orchestrator"
MODEL_ACCESS = (
    "epistrel.orchestrator.models.access",
    "epistrel.orchestrator.models.adapter",
    "epistrel.orchestrator.models.openai_compatible",
    "epistrel.orchestrator.models.structured",
)
"""Importing one of these (or a name from ``epistrel.orchestrator.models``) makes a module a
model caller."""
UNGUARDED = frozenset(
    {
        "epistrel.orchestrator.models.stub",  # the stub adapter: no real model call
        "epistrel.orchestrator.models.metrics",  # Prometheus metric definitions
    }
)
TEMPLATE_DIGESTS = frozenset(
    {
        "epistrel.orchestrator.models.facts.TEMPLATE_HASHES",
        "epistrel.orchestrator.images.output_check.TEMPLATE_SHA256",
    }
)
"""Template hashes: the fingerprint carries the templates already, so a template edit moves it
without a revision bump."""
EXTRA_MODULES = (
    "epistrel.orchestrator.models.registry",
    "epistrel.orchestrator.models.provider_schema",
    "epistrel.orchestrator.policies.seeds.policies",
    "epistrel.orchestrator.rewrite.support_check",
    "epistrel.orchestrator.lite.schemas",
    "epistrel.orchestrator.structured.prompt",
    "epistrel.core.summary",
)
EXTRA_NAMES = (
    "epistrel.core.settings_schema.LENGTH_TOKENS",
    "epistrel.core.settings_schema.MAX_LENGTH_TOKENS",
    "epistrel.core.settings_schema.DEFAULT_LITE_SUMMARY_MAX_TOKENS",
    "epistrel.core.states.effects.CLOCK_LINE",
)
SOURCES = (
    "epistrel.orchestrator.models.provider_schema.is_anthropic",
    "epistrel.orchestrator.models.provider_schema.strict_wire_schema",
    "epistrel.orchestrator.models.provider_schema.json_schema_mode",
    "epistrel.orchestrator.models.provider_schema._strip",
    "epistrel.orchestrator.models.openai_compatible.request_body",
    "epistrel.orchestrator.models.openai_compatible.provider_headers",
    "epistrel.orchestrator.models.openai_compatible.sends_thinking",
    "epistrel.orchestrator.models.openai_compatible.sdk_args",
    "epistrel.orchestrator.models.structured.with_headroom",
    "epistrel.orchestrator.compose.compose.polish_params",
    "epistrel.orchestrator.lite.prompt.model_params",
    "epistrel.orchestrator.structured.render.render_params",
    "epistrel.core.states.effects.syntax",
)


def _module_name(path: Path) -> str:
    relative = path.relative_to(REPO_ROOT / "src").with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts.pop()
    return ".".join(parts)


def _imports_model_access(tree: ast.Module) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if node.module in (*MODEL_ACCESS, "epistrel.orchestrator.models"):
                return True
        elif isinstance(node, ast.Import) and any(a.name in MODEL_ACCESS for a in node.names):
            return True
    return False


def guarded_modules() -> list[str]:
    """Every guarded module, sorted."""
    found = set(EXTRA_MODULES)
    for path in sorted(ORCHESTRATOR.rglob("*.py")):
        tree = ast.parse(path.read_text("utf-8"))
        if _imports_model_access(tree):
            found.add(_module_name(path))
    return sorted(found - UNGUARDED)


_LITERAL = object()
_MUTABLE = (dict, list, set)


def _assigned(module: str) -> dict[str, Any]:
    """Each upper-case module-level name assigned in ``module``: its literal value when the
    assignment is a literal (``ast.literal_eval``), else ``_LITERAL`` (read it at run time)."""
    source = inspect.getsource(importlib.import_module(module))
    names: dict[str, Any] = {}
    for node in ast.parse(source).body:
        targets: list[ast.expr] = []
        value: ast.expr | None = None
        if isinstance(node, ast.Assign):
            targets, value = list(node.targets), node.value
        elif isinstance(node, ast.AnnAssign):
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name) and target.id.isupper() and value is not None:
                try:
                    names[target.id] = ast.literal_eval(value)
                except ValueError, SyntaxError, TypeError:
                    names[target.id] = _LITERAL
    return names


def _value(loaded: Any, name: str, literal: Any) -> Any:
    """A constant's value: its literal, else its run-time value when that is immutable. A
    computed ``dict``, ``list`` or ``set`` is skipped: it may be a registry other modules fill
    (``images.jobs._TARGETS``), so its run-time value depends on what was imported."""
    if literal is not _LITERAL:
        return literal
    value = getattr(loaded, name, NotImplemented)
    return NotImplemented if isinstance(value, _MUTABLE) else value


def _plain(value: Any) -> Any:
    """``value`` as JSON-able plain data, or ``NotImplemented`` when it is not plain data."""
    if isinstance(value, Enum):
        return NotImplemented
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (tuple, list)):
        items = [_plain(v) for v in value]
        return NotImplemented if NotImplemented in items else items
    if isinstance(value, (set, frozenset)):
        items = [_plain(v) for v in value]
        if NotImplemented in items:
            return NotImplemented
        return sorted(items, key=canonical_json)
    if isinstance(value, Mapping):
        pairs = {str(k): _plain(v) for k, v in value.items() if isinstance(k, (str, int, Enum))}
        if len(pairs) != len(value) or NotImplemented in pairs.values():
            return NotImplemented
        return pairs
    return NotImplemented


def _digest(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _resolve(dotted: str) -> Any:
    module, _, name = dotted.rpartition(".")
    return getattr(importlib.import_module(module), name)


def entries() -> dict[str, str]:
    """The guarded set: each entry's key and the SHA-256 of its value."""
    found: dict[str, str] = {}
    for module in guarded_modules():
        loaded = importlib.import_module(module)
        for name, literal in _assigned(module).items():
            if f"{module}.{name}" in TEMPLATE_DIGESTS:
                continue
            plain = _plain(_value(loaded, name, literal))
            if plain is not NotImplemented:
                found[f"const:{module}.{name}"] = _digest(plain)
    for dotted in EXTRA_NAMES:
        plain = _plain(_resolve(dotted))
        assert plain is not NotImplemented, dotted
        found[f"const:{dotted}"] = _digest(plain)
    from epistrel.orchestrator.models.registry import DELIVERED

    for name, purpose in sorted(DELIVERED.items()):
        response = purpose.response_schema
        found[f"schema:{name}"] = _digest(
            {
                "input": purpose.input_schema.model_json_schema(),
                "response": None if response is None else response.model_json_schema(),
                "history_field": purpose.history_field,
                "variants": sorted(purpose.variants),
            }
        )
    for dotted in SOURCES:
        found[f"source:{dotted}"] = _digest(inspect.getsource(_resolve(dotted)))
    return dict(sorted(found.items()))


def read_snapshot(path: Path = SNAPSHOT) -> dict[str, Any]:
    document: dict[str, Any] = json.loads(path.read_text("utf-8"))
    return document


def check_snapshot(path: Path = SNAPSHOT) -> None:
    """Assert the guarded set equals the snapshot's and the snapshot pins the current
    revision (``tests/test_model_call_revision.py``, C-18's M11 part)."""
    snapshot = read_snapshot(path)
    current = entries()
    pinned: dict[str, str] = snapshot["entries"]
    differs = {
        "added": sorted(set(current) - set(pinned)),
        "removed": sorted(set(pinned) - set(current)),
        "changed": sorted(k for k in set(current) & set(pinned) if current[k] != pinned[k]),
    }
    how = (
        "bump MODEL_CALL_REVISION, then `uv run python -m tests.model_call_support --write`"
        if snapshot["revision"] == MODEL_CALL_REVISION
        else "`uv run python -m tests.model_call_support --write`"
    )
    assert differs == {"added": [], "removed": [], "changed": []}, (
        f"what a model call sends changed ({how}): {differs}"
    )
    assert snapshot["revision"] == MODEL_CALL_REVISION, (
        f"MODEL_CALL_REVISION is {MODEL_CALL_REVISION} but the snapshot pins "
        f"{snapshot['revision']}: {how}"
    )


def write_snapshot(path: Path = SNAPSHOT) -> str:
    """Rewrite the snapshot at the current revision. Refused when the guarded set changed and
    the revision did not move."""
    current = entries()
    added: list[str] = []
    if path.is_file():
        old = read_snapshot(path)
        pinned: dict[str, str] = old["entries"]
        if old["revision"] >= MODEL_CALL_REVISION:
            added = sorted(set(current) - set(pinned))
        kept = {key: current[key] for key in pinned if key in current}
        if kept != pinned and old["revision"] >= MODEL_CALL_REVISION:
            changed = sorted(set(pinned.items()) ^ set(kept.items()))
            raise SystemExit(
                f"the guarded set changed ({len(changed)} entries differ) but "
                f"MODEL_CALL_REVISION is still {MODEL_CALL_REVISION}: bump it first"
            )
    document = {"revision": MODEL_CALL_REVISION, "entries": current}
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(document, indent=1, sort_keys=True) + "\n", "utf-8")
    note = f" (added without a bump: {', '.join(added)})" if added else ""
    return f"wrote {len(current)} entries at revision {MODEL_CALL_REVISION} to {path}{note}"


if __name__ == "__main__":
    if sys.argv[1:] != ["--write"]:
        raise SystemExit("usage: python -m tests.model_call_support --write")
    print(write_snapshot())
