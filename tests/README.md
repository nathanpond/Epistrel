# Tests

`uv run pytest` runs everything; `-m "not integration"` skips the tests that need PostgreSQL (see the
root README for the database options). This file documents the **milestone part markers** that
`tools/gates check-parts` enforces in CI (docs/04 §13, docs/03 §22).

## Marking a test with a milestone part

```python
import pytest


@pytest.mark.part("M2", "FR-STORE-10..12; FR-CONC-1,2")
def test_i_10__event_log() -> None: ...
```

- `part(milestone, requirements, *, interim_until=None)` — all three are **string literals**
  (the checker reads tests by AST and never imports them).
- `milestone` is a §22 row (`M0`–`M8`) and says which milestone the part gates from.
- `requirements` lists what the part covers: IDs, `..` ranges and `,` lists, groups separated by `;`.
  Every ID must be defined in docs/03 and assigned to this milestone **or an earlier one**; naming a
  later requirement fails CI (`later requirement …`).
- `interim_until="M7"` marks a part that a later milestone retires (docs/03 §22 "Upgrades before
  M7"); it must name a later §22 milestone. Recorded now; enforcement arrives with the §15 check (#40).
- A test may carry **several** `part` markers when docs/04 splits it across milestones (U-01 has M4
  and M5 parts) — one marker per milestone, never two for the same one. Class- and module-level
  `part` markers (`pytestmark`) are rejected.

### Unmarked tests

A test without a `part` marker may name requirement IDs in its **docstring** (`FR-RESP-1`,
`FR-CONC-1..2`); it then gates from the latest milestone among them. A test that names nothing is
not a part and is ignored by the checker.

### Test names ↔ docs/04 test IDs

The function name is the docs/04 test ID, lowercased, with `-` → `_`:

| docs/04 | function |
|---|---|
| `I-10` | `test_i_10` |
| `S-ERASURE-BOUNDARY` | `test_s_erasure_boundary` |
| property test `OUTBOX` | `test_outbox` |

When one docs/04 test is implemented as several functions (its parts land in different stories),
suffix each slice with `__<slice>`: `test_i_18__pins`, `test_i_18__summary`. The mapper strips the
suffix, so every slice maps to `I-18`. A function with several `part` markers is checked against the
capability lexicon (#39) at the **latest** of its milestones.

### What CI checks (`uv run python -m tools.gates check-parts --json`)

- every marker's milestone exists in §22; every requirement is defined and not assigned later;
- every **M**-priority requirement assigned at or before `[tool.epistrel.gates] current_milestone`
  (pyproject.toml) is covered by a part gating at or before that milestone — `uncovered FR-X-n (M, Mk)`
  otherwise;
- `build/parts.json` (one entry per part: `node_id`, `test_id`, `milestone`, `requirements`,
  `interim_until`, `source`, `file`, `line`, `origin`) is written on success for the lexicon (#39) and
  §15 (#40) checks.

`tests/gates/fixtures/` contains deliberately broken trees for the checker's own tests; pytest does
not collect them (`norecursedirs`).
