"""check-epistemic-separation (#159; FR-EPI-1): the canon store never reads the belief tables and
the epistemic model never reads ``facts``. The repository passes, and the scan catches each kind
of read on each side, and nothing in prose."""

from __future__ import annotations

from pathlib import Path

from tools.gates.__main__ import main as gates_main
from tools.gates.epistemic import CANON_SIDE, EPISTEMIC_SIDE, main, scan, writes

CANON_READS_BELIEFS = '''"""Canon that mentions SELECT * FROM beliefs in prose only here."""
from sqlalchemy import select, text

from epistrel.core.epistemic import partitions
from epistrel.core.tables import beliefs
import epistrel.core.epistemic


def reads(conn):
    a = select(beliefs).where(beliefs.c.holder_character_id == "x")
    b = text("SELECT b.id FROM facts f JOIN belief_state b ON b.belief_id = f.id")
    return a, b, partitions
'''

PARTITION_READS_FACTS = '''"""A partition that says FROM facts in prose only."""
from sqlalchemy import select, text

from epistrel.core.canon import facts as store
from epistrel.core.tables.facts import facts


def reads(conn):
    return select(facts.c.id), text("SELECT id FROM facts"), store
'''

CLEAN = '''"""Reads its own table only (``FROM facts`` and ``FROM beliefs`` in prose are fine)."""
from epistrel.core.tables import propositions


def reads():
    return propositions.c.id
'''


def _tree(tmp_path: Path, files: dict[str, str]) -> Path:
    for rel, body in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body, encoding="utf-8")
    return tmp_path


def test_the_repository_keeps_canon_and_partitions_apart() -> None:
    assert scan() == []
    assert writes() == []
    assert main([]) == 0
    assert gates_main(["check-epistemic-separation"]) == 0


def test_a_canon_read_of_the_belief_tables_is_found(tmp_path: Path) -> None:
    root = _tree(tmp_path, {f"{CANON_SIDE.package}/reads.py": CANON_READS_BELIEFS})
    found = scan(root, (CANON_SIDE,))
    lines = sorted(int(f.split(":")[1]) for f in found)
    assert lines == [4, 5, 6, 10, 11]
    assert all("FR-EPI-1" in f for f in found)


def test_a_partition_read_of_facts_is_found(tmp_path: Path) -> None:
    root = _tree(tmp_path, {f"{EPISTEMIC_SIDE.package}/reads.py": PARTITION_READS_FACTS})
    found = scan(root, (EPISTEMIC_SIDE,))
    assert sorted(int(f.split(":")[1]) for f in found) == [4, 5, 9, 9]  # facts.c and SQL


def test_prose_and_other_tables_are_not_reads(tmp_path: Path) -> None:
    root = _tree(
        tmp_path,
        {f"{CANON_SIDE.package}/clean.py": CLEAN, f"{EPISTEMIC_SIDE.package}/clean.py": CLEAN},
    )
    assert scan(root) == []


ROGUE_WRITER = '''"""Writes a belief around transition() (``_insert_belief`` in prose is fine)."""
from epistrel.core.epistemic import partitions
from epistrel.core.epistemic.partitions import _insert_belief


async def write(conn):
    return await partitions._insert_belief(conn), _insert_belief
'''

BELIEF_WRITES_CANON = '''"""A belief module that commits a fact."""
from epistrel.core.canon.store import commit_fact


async def leak(conn, membership):
    await commit_fact(conn)
    return membership.supersede
'''


def test_only_transition_writes_beliefs_and_no_belief_writes_canon(tmp_path: Path) -> None:
    """#160: ``_insert_belief`` named outside the two writer modules, and canon's writers named
    under ``core/epistemic``, are findings; the writer modules themselves are not."""
    root = _tree(
        tmp_path,
        {
            "src/epistrel/core/rogue.py": ROGUE_WRITER,
            f"{EPISTEMIC_SIDE.package}/leak.py": BELIEF_WRITES_CANON,
            f"{EPISTEMIC_SIDE.package}/transitions.py": ROGUE_WRITER,
        },
    )
    found = writes(root)
    rogue = sorted(int(f.split(":")[1]) for f in found if "rogue.py" in f)
    leak = sorted(int(f.split(":")[1]) for f in found if "leak.py" in f)
    assert rogue == [3, 7]
    assert leak == [2, 6, 7]
    assert not [f for f in found if "transitions.py" in f]


OBJECTIVE_LEAK = '''"""A context builder that asks what Bobby really believes."""
from epistrel.core.epistemic import nested
from epistrel.core.epistemic.nested import attribution_objective_relation


async def context(conn, row):
    found = await nested.attribution_objective_relation(conn, "s", "b", None, None)
    return found, attribution_objective_relation, row.attribution_objective_relation
'''


def test_only_the_verifier_reads_an_attributions_objective_relation(tmp_path: Path) -> None:
    """#167 (FR-BELIEF-15, NFR-PRIV-1): naming ``nested.attribution_objective_relation`` (a
    ``from`` clause or a call) outside ``core/epistemic/nested.py`` and the verifier is a
    finding, and (#563) so is a row's stored label of the same name; the verifier itself is
    clean."""
    root = _tree(
        tmp_path,
        {
            "src/epistrel/orchestrator/context/leak.py": OBJECTIVE_LEAK,
            "src/epistrel/orchestrator/verify/verifier.py": OBJECTIVE_LEAK,
        },
    )
    found = writes(root)
    leak = sorted(int(f.split(":")[1]) for f in found if "leak.py" in f)
    assert leak == [3, 7, 8]
    assert all("FR-BELIEF-15" in f for f in found if "leak.py" in f)
    assert sum("#563" in f for f in found) == 1  # the row's label; nested.'s call is #167's
    assert not [f for f in found if "verifier.py" in f]


LABEL_LEAK = '''"""A context builder that reads the stored objective relation every way it can."""
from epistrel.orchestrator.verify.verifier import AttributionCheck


def utterance(row):
    return row.attribution_objective_relation


def divergence(lie):
    return lie.attribution_objective_relation, getattr(lie, "attribution_objective_relation")


def asserted(claim):
    return claim.attribution_objective


def checked(c, check: AttributionCheck | None, made):
    built = AttributionCheck(None)
    return (
        c.result.attribution.objective,
        check.objective,
        built.objective,
        AttributionCheck.objective,
        getattr(c.attribution, "objective"),
        made.objective,
    )
'''

LABEL_WRITER = '''"""The utterance label's writer and a neighbour in the same module."""


def write_utterances(row):
    return row.attribution_objective_relation


def context(row):
    return row.attribution_objective_relation
'''


def test_the_stored_objective_relation_is_read_only_where_it_labels(tmp_path: Path) -> None:
    """#563 (FR-BELIEF-15, NFR-PRIV-1): each read shape of the stored label (a row's
    ``attribution_objective_relation``, ``AssertedClaim.attribution_objective``, and an
    ``AttributionCheck``'s ``objective`` through the class, an annotated or constructed name or
    an ``attribution`` receiver, or ``getattr``) is a finding outside the labelling and
    divergence readers; ``.objective`` on an unrelated object (``made``) is not. An allow-listed
    function is clean, while its module's other functions are not."""
    root = _tree(
        tmp_path,
        {
            "src/epistrel/core/epistemic/context.py": LABEL_LEAK,
            "src/epistrel/core/turns/beats.py": LABEL_WRITER,
            "src/epistrel/core/epistemic/divergences.py": LABEL_LEAK,
        },
    )
    found = writes(root)
    leak = sorted(int(f.split(":")[1]) for f in found if "context.py" in f)
    assert leak == [6, 10, 10, 14, 20, 21, 22, 23, 24]
    assert all("#563" in f and "FR-BELIEF-15" in f for f in found)
    beats = [f for f in found if "beats.py" in f]
    assert [int(f.split(":")[1]) for f in beats] == [9]  # context(), not write_utterances()
    assert not [f for f in found if "divergences.py" in f]
