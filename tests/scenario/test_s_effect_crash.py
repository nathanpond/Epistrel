"""S-EFFECT-CRASH: durable lifecycle and resume (docs/04 §7; FR-EXT-2,16..20, FR-CONC-6,
FR-RESP-8).

This file is shared by the stories that build the lifecycle: #215 (post-effect values, the
no-delta check), #212 (the staged turn, the crash boundaries and the end-to-end post-effect
scenario) and #213.

**No delta arithmetic** (``test_s_effect_crash__no_delta_apply``, #215, FR-EXT-20): the engine
never computes a resulting external value by applying a delta to the pre-effect read. Checked
on the code itself: the only writer of ``external_values`` is its projection, which folds the
three recorded sources (a read, a confirmation's resulting value, a post-effect read; and the
``Unknown`` when neither answered) by copying the value each event recorded, and neither the
fold nor the writers that stage those events does any arithmetic. Then on a story: the turn
reads 300, the purchase of 50 is confirmed with a resulting 230 (another writer spent too), so
version 2 is 230 (never the computed 250) and version 1 is unchanged.
"""

from __future__ import annotations

import ast
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.core.ids import new_ulid
from epistrel.core.membership import Position
from epistrel.orchestrator.providers import pinned
from epistrel.orchestrator.providers.port import WriteOutcome
from tests import hot_set_support as hs
from tests import provider_support as ps
from tests import state_support as ss

pytestmark = pytest.mark.integration

SRC = Path(__file__).resolve().parents[2] / "src" / "epistrel"
PROJECTION = SRC / "core" / "projections" / "external.py"
WRITERS = SRC / "orchestrator" / "providers" / "pinned.py"
CORE = SRC / "core" / "states" / "external.py"

FOLDS = {"value_row", "outcome_row", "_position", "_scope"}
"""The projection's row builders: the only code that produces an ``external_values`` row."""
WRITER_FUNCTIONS = {
    "Answer.pinned",
    "OutcomeTarget.pin",
    "Outcome.event",
    "stage_outcome",
    "_version",
    "write_confirmation_version",
    "post_effect_read",
    "record_unknown",
    "settle_value",
}
"""The writers that decide and stage a post-effect version's event (#215)."""
ARITHMETIC = (ast.Add, ast.Sub, ast.Mult, ast.Div, ast.FloorDiv, ast.Mod, ast.Pow)


def _arithmetic(function: ast.AST) -> list[ast.AST]:
    """The arithmetic in ``function``'s body (its annotations' ``X | None`` left out)."""
    body = [n for s in getattr(function, "body", []) for n in ast.walk(s)]
    return [
        n for n in body if isinstance(n, ast.AugAssign | ast.BinOp) and isinstance(n.op, ARITHMETIC)
    ]


WRITE_VERBS = {"insert", "update", "delete", "values"}


def _tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(), filename=str(path))


def _functions(tree: ast.Module) -> dict[str, ast.FunctionDef | ast.AsyncFunctionDef]:
    found: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    for node in tree.body:
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            found[node.name] = node
        elif isinstance(node, ast.ClassDef):
            for item in node.body:
                if isinstance(item, ast.FunctionDef | ast.AsyncFunctionDef):
                    found[f"{node.name}.{item.name}"] = item
    return found


def _names(node: ast.AST) -> set[str]:
    return {n.id for n in ast.walk(node) if isinstance(n, ast.Name)} | {
        n.attr for n in ast.walk(node) if isinstance(n, ast.Attribute)
    }


def _table_writes() -> list[str]:
    """Every place in ``src`` that writes ``external_values`` directly (an insert, update or
    delete statement naming it, SQL text writing it) or declares a projection of it."""
    found: list[str] = []
    for path in sorted(SRC.rglob("*.py")):
        where = str(path.relative_to(SRC))
        for node in ast.walk(_tree(path)):
            if isinstance(node, ast.Call):
                func = node.func
                verb = func.attr if isinstance(func, ast.Attribute) else getattr(func, "id", "")
                receiver = func.value if isinstance(func, ast.Attribute) else None
                named = [*node.args, *([receiver] if receiver is not None else [])]
                if verb in WRITE_VERBS and any(
                    isinstance(a, ast.Name) and a.id == "external_values" for a in named
                ):
                    found.append(f"{where}: {verb}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                text = " ".join(node.value.split()).upper()
                if any(
                    f"{verb} EXTERNAL_VALUES" in text
                    for verb in ("INSERT INTO", "UPDATE", "DELETE FROM")
                ):
                    found.append(f"{where}: sql")
            elif isinstance(node, ast.ClassDef):
                for item in node.body:
                    if (
                        isinstance(item, ast.Assign)
                        and any(isinstance(t, ast.Name) and t.id == "table" for t in item.targets)
                        and isinstance(item.value, ast.Name)
                        and item.value.id == "external_values"
                    ):
                        found.append(f"{where}: {node.name}")
    return found


def _no_delta_check() -> None:
    # 1. The projection is the only writer of the table.
    assert _table_writes() == ["core/projections/external.py: ExternalValuesProjection"]

    # 2. It folds exactly the three recorded sources, each row built by a row builder.
    folds = _tree(PROJECTION)
    functions = _functions(folds)
    apply = functions["ExternalValuesProjection.apply"]
    checked = {
        a.id
        for n in ast.walk(apply)
        if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "isinstance"
        for a in ast.walk(n.args[1])
        if isinstance(a, ast.Name)
    }
    assert checked == {"ExternalRead", "ExternalOutcome"}
    built = {
        n.func.id
        for n in ast.walk(apply)
        if isinstance(n, ast.Call) and isinstance(n.func, ast.Name) and n.func.id != "isinstance"
    }
    assert built == {"value_row", "outcome_row"}

    # 3. A row's value is the value its event recorded, copied: ``<payload>.value``.
    for name in ("value_row", "outcome_row"):
        (row,) = [n for n in ast.walk(functions[name]) if isinstance(n, ast.Dict)]
        values = {
            k.value: v
            for k, v in zip(row.keys, row.values, strict=True)
            if isinstance(k, ast.Constant)
        }
        copied = values["value"]
        assert isinstance(copied, ast.Attribute), name
        assert copied.attr == "value", name
        assert isinstance(copied.value, ast.Name), name
        assert copied.value.id == functions[name].args.args[1].arg, name

    # 4. No arithmetic and no delta anywhere a version's value is decided or folded.
    writers = _functions(_tree(WRITERS))
    for name, function in [
        *((n, functions[n]) for n in FOLDS),
        *((n, writers[n]) for n in WRITER_FUNCTIONS),
    ]:
        assert not _arithmetic(function), name
        assert "delta" not in _names(function), name

    # 5. The one sum on this path numbers versions; it never reads a value.
    base = _functions(_tree(CORE))["version_base"]
    assert "value" not in _names(base)


@pytest.mark.part("M6", "FR-EXT-20")
async def test_s_effect_crash__no_delta_apply(engine: AsyncEngine, states_delivered: None) -> None:
    """See the module docstring."""
    _no_delta_check()

    name = ps.unique_name("bank")
    provider = await ps.register(engine, ps.in_process_spec(name, writable=["gold"]))
    story, (cal,) = await hs.cast(engine, ["Cal"])
    await ss.set_level(engine, story, "on")
    gold = await ps.external_gold(engine, story, name)
    turn, read = await ps.record_read(engine, story, gold, cal, 300)
    (before,) = await ps.external_value_rows(engine, story)

    at = Position(turn, 0, 0, 2, 1)
    target = await pinned.outcome_target(engine, story, gold, cal, at)
    assert target is not None
    outcome = pinned.Outcome(
        outbox_id=new_ulid(),
        effect_id=new_ulid(),
        answer=WriteOutcome("confirmed", resulting_values={"gold": 230}),
        answered_at=datetime.now(UTC),
    )
    reads = pinned.PinnedReads(engine, story)
    async with ps.outcome_op(engine, story, turn=turn) as op:
        found = await pinned.settle_value(op, reads, target, outcome, provider)
        await op.commit()
    assert found is not None
    assert getattr(found, "value", None) == Decimal(230)  # the provider's, never 300 - 50
    assert reads.live_reads == 0

    first, second = await ps.external_value_rows(engine, story)
    assert first == before  # version 1 is never updated
    assert (first["version"], first["value"], first["base_event_id"]) == (1, 300, read)
    assert (second["version"], second["value"], second["base_event_id"]) == (2, 230, read)
    await ss.set_level(engine, story, None)
