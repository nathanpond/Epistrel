"""S-TRUST-BOUNDARY, M5 part (docs/04 §7; FR-AUTH-2; #178): Epistrel filters by viewpoint and
authorizes no one. The M2 to M4 parts are in ``tests/test_trust_boundary.py``; this one runs over
both surfaces (the ``both`` fixture), because its clause is that the rules hold identically over
REST and the library.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine

from tests import canon_support as c
from tests import partition_support as ps
from tests import perception_support as perception
from tests.conformance.conftest import Outcome, Surface
from tests.conformance.test_c_12 import THOUGHT, _rooms, _vp, _whisper_turn
from tests.conftest import REPO_ROOT
from tests.respond_support import body

pytestmark = pytest.mark.integration

CANARY = "S-TRUST-M5-CANARY-BROOCH"
SENTENCES = (
    "Epistrel sits behind the consumer, which decides who may inspect a story.",
    "Its privacy guarantee is stated per viewpoint, not per user: a character viewpoint is given "
    "only what that character could perceive, whoever asks for it.",
    "Story inspection (a character's beliefs, the divergences, the exposures and the promissory "
    "ledger) is an omniscient read only: a request that does not name `omniscient` is refused.",
)


def _shape(found: Outcome) -> tuple[int, str | None]:
    return found.status, found.code


@pytest.mark.part("M5", "FR-AUTH-2")
async def test_s_trust_boundary__m5(
    both: dict[str, Surface],
    engine: AsyncEngine,
    drained_embedding_jobs: None,
) -> None:
    """A character-viewpoint respond or history read never returns another character's private
    content: Bobby is given neither Jenni's thought nor her belief about the canary brooch (nor
    its id), under either of two principals, with the same bodies. Partition, belief,
    divergence, exposure and audit reads that do not name ``omniscient`` are rejected with the
    same status and code over REST and the library. The README and docs/02 §11 phrase the
    privacy guarantee per viewpoint, not per user (exact sentences). Complement: ``omniscient``
    named by the same request is answered."""
    rest, library = both["rest"], both["library"]
    stub = rest.engine.models.stub
    story, ids, places = await _rooms(rest, engine, "co_location")
    brooch = await perception.place_entity(engine, story, CANARY)
    held = await ps.believe(engine, story, ids["Jenni"], c.at(brooch, places["parlor"], 0, None))
    stub.script("generation", [_whisper_turn(ids)])
    played = await rest.respond(story, body(ids["Wren"], "Well?"), viewpoint=_vp(ids["Bobby"]))
    assert played.status == 200, played.code
    for found in (json.dumps(played.json),):
        assert THOUGHT not in found
        assert CANARY not in found
        assert held.id not in found
    reads: dict[str, Any] = {}
    for name, surface in both.items():
        for principal in ("p-one", "p-two"):
            read = await surface.history(story, viewpoint=_vp(ids["Bobby"]), principal=principal)
            assert read.status == 200, read.code
            text = json.dumps(read.json)
            assert THOUGHT not in text
            assert CANARY not in text
            assert held.id not in text
            reads[f"{name}:{principal}"] = read.json
    assert reads["rest:p-one"] == reads["rest:p-two"]
    assert reads["library:p-one"] == reads["library:p-two"]

    character = _vp(ids["Bobby"])
    asks: list[tuple[str, dict[str, Any]]] = [
        ("beliefs", {"character": ids["Jenni"]}),
        ("divergences", {}),
        ("exposures", {}),
        ("ledger", {}),
    ]
    for kind, params in asks:
        for viewpoint, expected in ((character, 403), (None, 422)):
            outcomes = [
                _shape(await s.inspect(kind, story, viewpoint=viewpoint, **params))
                for s in (rest, library)
            ]
            assert outcomes[0] == outcomes[1], (kind, viewpoint)
            assert outcomes[0][0] == expected, (kind, viewpoint, outcomes)
        named = [
            (await s.inspect(kind, story, viewpoint="omniscient", **params)).status
            for s in (rest, library)
        ]
        assert named in ([200, 200], [409, 409]), (kind, named)  # the ledger: no commitment
    audits = [
        _shape(await s.history(story, viewpoint=character, include_audit=True))
        for s in (rest, library)
    ]
    assert audits == [(403, "omniscient_required")] * 2
    seen = await library.inspect("beliefs", story, viewpoint="omniscient", character=ids["Jenni"])
    assert held.id in {b["belief_id"] for b in seen.json["beliefs"]}

    readme = (REPO_ROOT / "README.md").read_text()
    section = readme.split("## Trust boundary", 1)[1].split("\n## ", 1)[0]
    architecture = (REPO_ROOT / "docs" / "02-technology-architecture.md").read_text()
    section_11 = architecture.split("## 11. Configuration", 1)[1].split("\n## ", 1)[0]
    for sentence in SENTENCES:
        assert sentence in section, sentence
        assert sentence in section_11, sentence
