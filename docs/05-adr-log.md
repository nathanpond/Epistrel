# Epistrel — Architecture Decision Record Log

**Working draft**

Each ADR records a decision, its context, and consequences, so the *why* travels with the project. Status values: **Accepted**, **Superseded**, **Proposed**. ADRs are append-only: when a later ADR changes part of an earlier one, the earlier ADR gains an **Amended by** line naming the later ADR and the clause it changes, and clauses that are now wrong carry an inline *[superseded by ADR-…]* marker. Where a later ADR narrows an earlier ADR's **Consequences**, that line is restated in its narrowed form and marked *[revised per ADR-…]*, so no broader claim remains readable as current. The current rule is always the latest ADR in the chain; the requirements specification (*03*) is normative where the two differ. The **indexes** below are generated from the log on every change: by requirement area, and by ADR with each one's standing — current, amended, or partly superseded.

---

## Indexes

**Implementing from this log.** Build from the requirements specification (*03*), which is normative, and use this log for the reasons behind it. Inline bracketed markers are the only text in an older ADR that states a current rule; the rest of an amended ADR is history. Both indexes are generated from the log on every change.

### By requirement area

The ADRs whose decisions cite each requirement area, oldest first — the last listed is the most recent decision touching that area. ADRs that predate requirement IDs appear only in the index by ADR.

| Area | Spec | ADRs |
|---|---|---|
| FR-RESP | §1 | 047, 071, 078, 087, 091, 144, 145 |
| FR-STORE | §2 | 045, 072, 075, 076, 079, 080, 122, 128, 129, 132, 138, 143, 144 |
| FR-CONC | §2.3 | 045, 052, 057, 062, 072, 144 |
| FR-MEM | §4 | 070, 071, 072, 075 |
| FR-EPI | §5 | 024 |
| FR-BELIEF | §5.1 | 026, 069, 075 |
| FR-TURN | §6 | 043 |
| FR-COMMIT | §7 | 018, 022, 043, 053, 129 |
| FR-RET | §8 | 129 |
| FR-VERIFY | §9 | 043, 065, 077, 138, 144 |
| FR-GUARD | §11 | 087, 095, 096, 114 |
| FR-REWRITE | §12.1 | 144 |
| FR-MODEL | §13 | 129, 136, 144 |
| FR-EXT | §15 | 051, 054, 056, 061, 066, 072, 074, 092, 133, 140, 144 |
| FR-STATE | §15.1 | 055, 072, 144 |
| FR-CLOCK | §15.2 | 060, 063, 064, 067, 068, 073, 074, 076, 144 |
| FR-MULTI | §16 | 043, 126 |
| FR-API | §17 | 092, 138 |
| FR-AUTH | §17.1 | 078, 091, 093, 144 |
| FR-ADMIN | §18 | 046, 080, 103, 128, 129, 138, 144 |
| FR-CAT | §18.1 | 081, 088, 089, 090, 091, 095, 097, 107, 108, 138, 144 |
| FR-SCN | §18.2 | 081, 082, 087, 088, 091, 097, 107, 140, 141, 142 |
| FR-PROF | §18.3 | 084, 086, 087, 107, 134, 138, 141, 142, 144 |
| FR-LITE | §18.4 | 085, 086, 087 |
| FR-MEDIA | §18.5 | 083, 097, 113, 116 |
| FR-RATE | §18.6 | 094, 107, 132, 138 |
| FR-IMG | §18.7 | 098, 099, 100, 101, 102, 103, 104, 105, 108, 109, 110, 114, 116, 117, 118, 119, 120, 121, 124, 125, 129, 130, 131, 135, 136, 145 |
| FR-FB | §18.8 | 111 |
| FR-OBS | §19 | 129 |
| NFR-PERF | §20 | 080, 123, 126, 133, 137, 145, 146, 147 |
| NFR-DETERM | §20 | 045, 080 |
| NFR-SCALE | §20 | 070, 072, 075, 137 |
| NFR-QUAL | §20 | 044, 065, 077, 080, 101, 106, 112, 114, 115 |
| NFR-REL | §20 | 045, 127 |
| NFR-SEC | §20 | 138 |
| NFR-PRIV | §20 | 077, 101 |
| NFR-OPS | §20 | 127 |

### By ADR

**Current** — unchanged since it was accepted. **Amended** — read it together with the ADRs listed; changed clauses carry inline markers. **Partly superseded** — some clauses are replaced; the inline markers name the ADR that governs them.

| ADR | Title | Standing | Read with |
|---|---|---|---|
| 001 | LLM as renderer; authoritative state is external | Amended | 077 |
| 002 | Single transactional store (PostgreSQL + pgvector) | Amended | 054, 127, 137, 145 |
| 003 | Event sourcing + bitemporal canon | Amended | 027, 080, 122 |
| 004 | Epistemic-first: per-character viewpoint is the core primitive | Current | — |
| 005 | Product boundary: Epistrel = Engine Core + Orchestrator; the consumer is thin | Current | — |
| 006 | Engine Core is deterministic and model-free; deterministic-first, cognition-on-the-residual | Amended | 017, 035 |
| 007 | Model Access Layer: per-purpose config held in Epistrel; Epistrel connects directly | Amended | 136 |
| 008 | Structured turn contract (say/think/do/latent); utterances as speech acts | Amended | 036 |
| 009 | Commitment lifecycle: consequence-driven, salience-triggered, per-knower | Partly superseded | 022, 043, 053 |
| 010 | Validation and repair: three-way verify, viewpoint-aware, least-invasive repair | Amended | 035, 065, 087 |
| 011 | Guardrails: immutable policy, policy-never-yields, in-character enforcement | Amended | 094, 095, 096, 098 |
| 012 | Directives and bitemporal retcon under an authority hierarchy | Current | — |
| 013 | Orchestrator extensibility; external data as a first-class fact authority | Amended | 030 |
| 014 | Multi-character single-response composition | Amended | 031, 047, 059 |
| 015 | Model-agnostic via OpenAI-compatible interface; Apache-2.0 license | Current | — |
| 016 | API surfaces: REST + MCP + library; Story as the top-level container | Partly superseded | 092 |
| 017 | Deterministic-first job disposition; the model is reached only through measured, tunable gates | Amended | 065, 080, 084 |
| 018 | In-story time travel via sequence invalidation (truncate / regenerate) | Partly superseded | 027, 072, 122, 028 |
| 019 | Stories are self-contained; fork is a full copy; deletion is a scoped delete | Amended | 027, 072, 128 |
| 020 | Every external read is an event; single source of truth for external values | Amended | 030, 056 |
| 021 | Natural-language history rewrite with configurable, orthogonal modes | Amended | 027, 042, 053 |
| 022 | Determined/undetermined (world) and known/unknown (viewpoint) are separate axes; a determined fact has one value | Partly superseded | 049, 053, 035 |
| 023 | Character modeling: interaction-or-salience trigger; model liberally, weight conservatively; player has no interiority | Amended | 047 |
| 024 | Declared `think` is checked, not trusted; false beliefs require a permitted, antecedent-backed cause | Amended | 034, 036 |
| 025 | Belief formation is explicit, typed, and resolved as-of the exposure frame | Amended | 027, 034, 041, 069, 075 |
| 026 | Presence is not perception; route by an authoritative audience, with an envelope/content split | Amended | 043, 047, 071 |
| 027 | Three coordinates (narrative position, valid-time, transaction-time), explicit timeline membership, and amendments | Amended | 028, 042, 060, 072, 076, 079 |
| 028 | Event model: turn index ≠ seq; operations and categories; continuations and swap-on-undo | Amended | 041, 072, 086 |
| 029 | Optimistic concurrency, per-story turn policy, and idempotent mutations | Amended | 040, 045, 052 |
| 030 | Story States and owned values; authorities decide objective truth, not knowledge | Amended | 037, 038, 039, 060 |
| 031 | Multi-character interaction modes, participation roles, and adjudication | Amended | 047, 059, 126 |
| 032 | Closing verification and input gaps: verified composition, explicit input contract, novelty permissions, typed dependencies | Amended | 036, 043, 047, 059, 065, 087 |
| 033 | Consolidation scope, authorization, erasure boundary, and measurable acceptance | Partly superseded | 044, 045, 046, 070, 072, 071, 091, 103, 078 |
| 034 | Belief antecedents: independently verified and earlier, not "a prior turn" | Current | — |
| 035 | Normalized propositions, a layered predicate registry, and deterministic contradiction rules | Partly superseded | 048, 049, 050, 058 |
| 036 | Speech acts carry three independent dimensions: objective truth, speaker belief, and intent | Amended | 063 |
| 037 | Provider outages restrict new knowledge, not existing beliefs | Current | — |
| 038 | External effect lifecycle: membership-gated dispatch, no executable fork copies, irreversible confirmations, honest prose | Amended | 051, 054, 061, 066, 072 |
| 039 | State effects need authorization, action preconditions, transfers, and two-way completeness | Amended | 055, 144 |
| 040 | Read sets are query keys, including empty results | Amended | 052 |
| 041 | Lazy belief resolutions: story truth, with exposure and resolution anchors | Amended | 075 |
| 042 | Amendment undo is a checked operation; corrections attach to propositions or exact records | Amended | 079 |
| 043 | Ledger obligations per proposition; reactive narration exempt from novelty; widening perception validated | Current | — |
| 044 | Separate gate rules for rate floors and zero-tolerance floors | Amended | 077 |
| 045 | Replay determinism excludes live re-embedding; idempotency has a result window and permanent tombstone | Current | — |
| 046 | Erasure is a control action, guarded at every write path, with a ledger that survives restores | Amended | 080, 128 |
| 047 | Explicit output viewpoint with an audience filter; disclosure is distinct from inference | Partly superseded | 059, 071, 091, 078, 145 |
| 048 | Support requires coverage; instants are first-class | Partly superseded | 058 |
| 049 | Functional uniqueness is over a declared key; distinct facts stay distinct; exhaustiveness only by declared closed sets | Current | — |
| 050 | Weak predicates forbid inferred exclusivity, not direct negation | Current | — |
| 051 | External effects follow a staged, durable lifecycle with a recorded in-flight point | Amended | 054, 055, 062 |
| 052 | Configuration is versioned per operation; interpretive changes revalidate, prospective changes don't | Amended | 057, 062, 072, 084 |
| 053 | Thunk resolution is checked for retroactive obligations; author authority picks the outcome, not a bypass | Current | — |
| 054 | Ordinary turns are atomic; synchronous-effect turns are workflows of atomic stages | Amended | 062, 072 |
| 055 | Internal effects contingent on external ones are unconditional, reserved, or conditional | Amended | 061, 066 |
| 056 | Confirmations create post-effect value versions; deltas are never applied to stale reads | Current | — |
| 057 | Configuration rebase cascades through same-turn dependencies | Current | — |
| 058 | Exact equality, coarse compatibility, and event identity are separate temporal relations | Current | — |
| 059 | Composition checks are viewpoint-specific; stored results are re-authorized on every read | Partly superseded | 091 |
| 060 | A built-in world clock: deterministic pacing, bounded skips, scheduled-event checks | Partly superseded | 063, 064, 067, 068, 073 |
| 061 | Late settlements take effect where they arrive, re-verified, never backdated | Partly superseded | 066, 072 |
| 062 | Configuration rebasing stops at committed stages | Partly superseded | 072 |
| 063 | Time claims use the three speech dimensions; characters get only the time they can know | Current | — |
| 064 | Scheduled events have a lifecycle; an interrupted skip is regenerated at the cutoff; beliefs don't stop the clock | Partly superseded | 073 |
| 065 | Claim detection is layered, always on by default, and measured separately from verification | Amended | 077, 080 |
| 066 | One settled terminal outcome per effect; expired reservations never convert automatically | Current | — |
| 067 | Beats and steps have start and end times | Amended | 074, 076 |
| 068 | Partial anchors: an exact offset plus a ranged absolute anchor; never invent precision | Amended | 073, 079 |
| 069 | Nested beliefs are explicit records, verified only against the holder's own view | Current | — |
| 070 | Retrieval eligibility is separate from timeline membership; retirement is mandatory and budgeted | Partly superseded | 072, 075 |
| 071 | Viewpoint-filtered working memory; consolidation preserves epistemic type | Current | — |
| 072 | Inter-turn slots; late settlement needs an active origin; durable outcome transactions; budgets that hold for every story | Partly superseded | 074, 075 |
| 073 | Creating a plan is not reporting a schedule; `possibly_due` is a refinement pause, not an interruption | Amended | 074 |
| 074 | Settlements take the clock at their slot; pacing advances are checked; `possibly_due` is defined over the anchor range | Amended | 076 |
| 075 | Story-wide pin cap and per-episode retirement deadlines; positions include inter-turn slots everywhere | Current | — |
| 076 | Simultaneous turns cut off after reconciliation; as-originally-recorded views at slot boundaries | Current | — |
| 077 | "The model proposes; the engine validates": a stated guarantee boundary, with privacy structural first | Amended | 084, 106, 080, 087 |
| 078 | Request roles are separate fields with separate authorization | Partly superseded | 091 |
| 079 | Retcon eligibility is per correction part, in clock offsets where possible, with a configurable rule for undecidable timing | Current | — |
| 080 | Per-turn model budgets and deadlines with ordered degradation; honest claims about leaks and erasure | Partly superseded | 122, 123, 133, 087, 144 |
| 081 | A versioned character catalog, used by snapshot; every character gets a companion scenario | Partly superseded | 088, 090, 094, 095, 097, 098 |
| 082 | Scenarios with role slots, a configurable starting-cast limit, deterministic seed merging, and an atomic genesis | Partly superseded | 087, 088, 094, 141, 091 |
| 083 | Media as immutable assets in a pluggable blob store | Amended | 094, 097, 098, 113 |
| 084 | Engine profiles: capability modules with validated dependencies, followed live, changed by warn-then-confirm | Partly superseded | 087, 091, 134, 138, 144 |
| 085 | A Lite pipeline that matches today's roleplay engines, on the same catalog and transcript | Amended | 087 |
| 086 | Backfill re-derives from the stored transcript, pauses the story, and resolves contradictions by policy | Current | — |
| 087 | Precise output claims, Lite's call boundary, seed merging by type, and certified configurations | Amended | 107, 142, 088 |
| 088 | Facts from the scenario, personality from the character; personas are not catalog characters; only public cards are public | Partly superseded | 089, 091, 107, 142, 090, 141 |
| 089 | Persona details are seeded by audience, like any other knowledge | Amended | 107 |
| 090 | Character and companion versions are separate, pinned, and published atomically | Amended | 094 |
| 091 | Epistrel authorizes no end users: service authentication, attribution and ownership, and privacy per requested viewpoint | Current | — |
| 092 | No MCP server | Current | — |
| 093 | Principal offboarding works type by type | Current | — |
| 094 | Content ratings are deployment-defined guidance plus guardrails; a story's rating changes only on request | Amended | 098, 107, 132 |
| 095 | Every character has a true age; a seeded global policy protects minors at every rating | Amended | 098, 108 |
| 096 | Global guardrails screen text coming into a story | Current | — |
| 097 | Images are optional; characters carry a visual identity; large media are delivered in pieces | Amended | 108 |
| 098 | Image generation is an optional core capability behind a port; image guardrails fail closed | Partly superseded | 100, 101, 102, 104, 108, 109, 110 |
| 099 | Story images are anchored at narrative positions, made for a viewpoint, and follow the timeline | Amended | 100, 101, 103, 105, 110 |
| 100 | Generated images are staged until checked, and image jobs are bound to their origin | Partly superseded | 104, 109, 110 |
| 101 | Story-image privacy is a guarantee about the image model's inputs; image checking is a measured backstop | Amended | 105, 110, 108 |
| 102 | The image adapter discovers capabilities per endpoint and records what it actually applied | Amended | 118, 131 |
| 103 | Images declare who they depict; erasure reaches images only through declared or named subjects | Current | — |
| 104 | Image limits count every provider output; catalog results are candidates attached deliberately | Amended | 109, 135, 138 |
| 105 | Image metadata states its guarantees; an image's audience viewpoint is separate from its camera POV | Amended | 110 |
| 106 | Creative quality is outside the guarantee; Epistrel guarantees the inputs | Amended | 112 |
| 107 | Review clarifications: persona text is never canon, one validation report shape, stale versus uncertified, advisory rating suggestions | Amended | 141 |
| 108 | The image safety path knows true ages; the image model gets only what the audience perceives | Amended | 114, 129 |
| 109 | Catalog candidates are durable private assets; image reservations are atomic; unchecked uploads are named as such | Partly superseded | 113, 114, 117, 135 |
| 110 | Checker certification is reported per policy; image tests and ADR consequences stay inside the guarantee boundary | Current | — |
| 111 | Players can rate messages; feedback is a flag, never an input | Current | — |
| 112 | The engine must not degrade prose; voice, plot, repetition, and length are reported, not gated | Amended | 114, 115 |
| 113 | The general media sweep spares drafts and candidates; the images introduction separates the three sources | Current | — |
| 114 | Precise wording for age checks and the prose floor; image attempts are fenced | Amended | 116, 117, 125, 115 |
| 115 | The prose floor needs enough evidence per intervention type | Current | — |
| 116 | Fenced image attempts settle their own reservation through a dedicated path | Amended | 117, 119, 121, 124 |
| 117 | After an attempt's horizon, retries wait only when the adapter can confirm completion; overage is the only way past a limit | Amended | 118, 125, 120 |
| 118 | `confirms_completion` is a declared adapter capability with a strict meaning of "definitive" | Partly superseded | 119, 124, 125, 131 |
| 119 | A confirmed success is adopted, not retried; `cancel` is part of the confirming contract | Amended | 120 |
| 120 | Adopted outputs after the horizon are overage, and status outputs beyond `n` are discarded unchecked | Amended | 121, 125, 129 |
| 121 | Image limit counters are defined sums over per-attempt reservation records | Amended | 124, 130, 129 |
| 122 | Compaction is an explicit, irreversible control operation | Current | — |
| 123 | Safety calls are reserved by formula; a turn that cannot fit them is planned smaller or rejected | Amended | 126, 133 |
| 124 | Image reservations snapshot their counters; provider delivery is at least once | Amended | 130, 131 |
| 125 | After the horizon, no-overlap holds only while a confirming job waits | Current | — |
| 126 | A fixed participation role is a bound; escalation that cannot happen is deferred | Current | — |
| 127 | Workers poll durably and own every timer; no external scheduler | Current | — |
| 128 | Stories export as versioned bundles and import only as new stories | Amended | 132, 143, 144 |
| 129 | Review clarifications: release gating for S and C requirements, one overage formula, and the image checker's data path | Amended | 136 |
| 130 | A presumed image settlement is provisional and is corrected once by the actual result | Current | — |
| 131 | The image port separates submission from collection; adapters declare `async` or `sync` | Current | — |
| 132 | Reactivated turns are re-checked against the guardrails in effect, including inactive turns from an import | Current | — |
| 133 | Guardrail calls are counted per text checked; a possible staged turn reserves its outcome step at planning | Current | — |
| 134 | Delivery runs in seven milestones, each gating the requirements it delivers | Amended | 138, 139, 141, 144 |
| 135 | Image limits have concrete defaults; zero disables; nothing is unlimited | Amended | 138 |
| 136 | One purpose registry generates the model guide; prompts and payloads use only declared inputs | Amended | 138 |
| 137 | Capacity claims stop at what has been measured | Amended | 145 |
| 138 | Milestone gates are scoped per test part; requirements that spanned milestones are split | Amended | 139, 140 |
| 139 | Infrastructure and project setup come first as M0 and M1; delivery milestones become M2–M8 | Amended | 140, 143, 144 |
| 140 | Test parts are checked against a capability lexicon; the remaining cross-milestone tests are split | Amended | 141 |
| 141 | Scenario fields have classes, and genesis materializes them by capability | Amended | 142 |
| 142 | Lite genesis does not detect fact conflicts; per-class handling, report projections, and forward-only seeds are explicit | Current | — |
| 143 | M4 routes nothing by perception; bundles carry the story's audit events and enforce explicit limits | Current | — |
| 144 | An exhaustive consistency pass: a complete capability map, perception levels, explicit pauses, and milestones released in order | Amended | 145 |
| 145 | The output viewpoint's channel filter applies everywhere, its audience filter only with perception; a reference workload for concurrency | Partly superseded | 146 |
| 146 | The concurrency gate is a fixed-schedule capacity test, staged by capability | Amended | 147 |
| 147 | The concurrency gate's service criteria hold per story | Current | — |

---

### ADR-001 — LLM as renderer; authoritative state is external
**Status:** Accepted.
**Amended by:** ADR-077 — "renderer" is superseded by "the model proposes; the engine validates"; several consequential steps are model-dependent and measured.
**Context:** In open roleplay the transcript *is* the world, which is why memory collapses and the model's errors become canon.
**Decision:** A language model renders prose from state and proposes state changes; it is never the source of truth. The authoritative world lives in Epistrel.
**Consequences:** Enables selective context, external verification, and a stable past. Requires a structured write path (ADR-008) and a validation layer (ADR-010).

---

### ADR-002 — Single transactional store (PostgreSQL + pgvector)
**Status:** Accepted.
**Amended by:** ADR-054 — whole-turn atomicity holds for ordinary turns only; turns with synchronous external effects commit in atomic stages; ADR-127 — workers poll durably and own every timed lifecycle job; no external scheduler; ADR-137 — capacity is claimed only for the measured range; ADR-145 — the concurrency target is defined by a reference host and workload
**Context:** Verification and atomic turn commits need world state and memory to be consistent at every instant.
**Decision:** One Postgres database holds events, projections, canon, beliefs, and vectors (pgvector); no separate vector DB.
**Consequences:** Atomic turns *[revised per ADR-054: ordinary turns; a turn with a synchronous external effect commits in atomic stages]*; no cross-store sync. Capacity is claimed only for the measured range — stories up to ~50k memories / ~100k events (NFR-SCALE-1, P-06) and 10+ concurrent stories on one host (NFR-PERF-4) *[revised per ADR-145: 10 stories in active play on the reference host under the reference workload]*; any larger figure, including the ~10M vectors once estimated here, is unvalidated until the capacity benchmark measures it. A graph backend and BM25 remain optional additions, not replacements. *[revised per ADR-137]*

---

### ADR-003 — Event sourcing + bitemporal canon
**Status:** Accepted.
**Amended by:** ADR-027 — `seq` is recording order only; history is addressed by narrative position, valid-time, transaction-time, and timeline membership; ADR-080 — erasure is the one exception to event immutability; replay is defined over the post-erasure log; ADR-122 — opt-in compaction is a second, explicit exception to event immutability.
**Context:** Long games need rewind, branching, audit, and clean retcon.
**Decision:** Append-only events are the truth; projections are derived caches. Facts are bitemporal (valid-time + transaction-time); supersession invalidates, never deletes.
**Consequences:** Save/load/rewind/branch nearly free; retcon reads as always-true in-story while auditable in record-time. Projections must be rebuildable (tested). (Refined in ADR-027: valid-time and transaction-time are joined by a third coordinate, narrative position, plus explicit timeline membership.)

---

### ADR-004 — Epistemic-first: per-character viewpoint is the core primitive
**Status:** Accepted.
**Context:** Generic agent-memory systems model agent+user or ACLs; they cannot hold secrets, false beliefs, or dramatic irony. This is the open problem for character AI.
**Decision:** Memory is partitioned by knower. Objective canon, self-beliefs, social/nested beliefs, false beliefs, and could-have-known constraints are modeled distinctly and enforced at retrieval.
**Consequences:** This is Epistrel's reason to exist. Drives visibility scoping, viewpoint-aware verification, the structured write path, and the asymmetry-correctness eval.

---

### ADR-005 — Product boundary: Epistrel = Engine Core + Orchestrator; the consumer is thin
**Status:** Accepted.
**Context:** An earlier draft placed the agent loop (verify/grade/re-roll) in the consumer. That would force every adopter to re-implement the hard, valuable part and reduce Epistrel to a database.
**Decision:** Both the **Engine Core** (deterministic data layer) and the **Orchestrator** (the agent) are inside Epistrel. The consumer sends `{StoryID, CharacterID(s), prompt params, scene context}`, registers external-data providers, supplies model config, and receives a finished message.
**Consequences:** "Almost no work for the consumer." The Orchestrator owns the turn loop; the Engine Core stays model-free (ADR-006).

---

### ADR-006 — Engine Core is deterministic and model-free; deterministic-first, cognition-on-the-residual
**Status:** Accepted.
**Amended by:** ADR-017 — model use only through measured gates; ADR-035 — contradiction is computed by registered predicate rules.
**Context:** Reproducibility underpins the regen/commit guarantees and testability; model calls are the source of cost, latency, and nondeterminism.
**Decision:** The Engine Core makes no model calls. Every model-using operation tries a deterministic path first (e.g., verify as set logic) and the Orchestrator invokes a model only on the fuzzy residual.
**Consequences:** Most hot-path verify calls never hit a model; the engine is testable with a stub adapter; escalation rate is a tracked metric.

---

### ADR-007 — Model Access Layer: per-purpose config held in Epistrel; Epistrel connects directly
**Status:** Accepted. **Supersedes** an earlier framing that put the generation call on the consumer's side.
**Amended by:** ADR-136 — purposes are declared in a registry that generates the model guide.
**Context:** Because the Orchestrator owns the turn loop and re-generates several times per turn, routing each sub-call back through the consumer is incoherent and breaks the thin-consumer contract. The earlier "consumer-side model" framing conflated *where the model runs* with *where its config lives*.
**Decision:** Model *processes* run externally (Ollama/vLLM/hosted) over the OpenAI-compatible wire. Epistrel **holds per-purpose configuration** (`base_url`, model, key, params) and **connects directly**, via a Model Access Layer (default adapter over LiteLLM). A **runtime config API** allows changes without restart. `base_url` is configurable for enterprise LLM gateways; a bring-your-own-completion escape hatch covers non-OpenAI-compatible models. "Model-agnostic" means configurable endpoints, not consumer-owned calls.
**Consequences:** Clean per-purpose routing (generation, regeneration, entailment-fallback, consolidation, importance, salience, extraction; embedding is a separate sticky port). Keys live in Epistrel config; for self-hosted OSS the adopter supplies keys to their own deployment.

---

### ADR-008 — Structured turn contract (say/think/do/latent); utterances as speech acts
**Status:** Accepted.
**Amended by:** ADR-036 — utterance truth is three independent labels (objective relation, belief relation, intent), not a single true/false.
**Context:** Free-text turns leave no basis for asymmetry, intent detection, or truth tracking.
**Decision:** Renders return `say`/`think`/`do`/`latent`, each routed by audience. Utterances are stored as speech acts (what was asserted), never promoted to truth; `think` is a declared-intent signal used to classify lie/false-belief/hallucination — checked against belief state, not trusted on its own (refined in ADR-024).
**Consequences:** Manufactures knowledge asymmetry turn by turn; enables the lie/false-belief/hallucination trichotomy; keeps lies from poisoning canon.

---

### ADR-009 — Commitment lifecycle: consequence-driven, salience-triggered, per-knower
**Status:** Accepted.
**Amended by:** ADR-022 — commitment is **global**, not per-knower ("per-knower" in this title is superseded: a fact is determined once for the world; characters differ only in knowing it); ADR-043 — ledger obligations are per proposition; ADR-053 — every thunk resolution passes a retroactive-obligation check.
**Context:** Truth must be stable (no retcon-on-reveal) yet regeneration must stay free for genuinely undecided outcomes.
**Decision:** A fact commits the instant any character's `think`/`do` depends on its value; salience (not event type) triggers commitment; commitment is per-knower *[superseded by ADR-022: commitment is global; characters differ only in knowing it]*; *hidden* (decided, sticky) is distinct from *undetermined* (a thunk). The commit boundary is the regeneration boundary.
**Consequences:** Regeneration varies the undetermined and preserves the determined. A promissory ledger prevents turns from closing with dangling signals.

---

### ADR-010 — Validation and repair: three-way verify, viewpoint-aware, least-invasive repair
**Status:** Accepted.
**Amended by:** ADR-035 — "contradicted" is defined by deterministic predicate rules; ADR-065 — verification checks every *detected* claim; detection recall is measured separately; ADR-087 — output claims are precise; Lite does no structured claim checking
**Context:** The model will contradict canon, fabricate, and break rules; naive find-replace repair breaks dependent reasoning.
**Decision:** `verify(claim, viewpoint)` returns supported/contradicted/novel (Engine Core may add uncertain). Repair picks the least-invasive rung; cascading contradictions are fixed by regenerating under the contradicted fact's neighborhood, not string substitution. Novel facts may be canonized.
**Consequences:** Nothing reaches the player unverified *[revised per ADR-065 and ADR-087: every detected claim is checked, under profiles that verify; Lite does no structured checking]*; the validator also grows canon; `fact_neighborhood` is a required primitive.

---

### ADR-011 — Guardrails: immutable policy, policy-never-yields, in-character enforcement
**Status:** Accepted.
**Amended by:** ADR-094 — policies are also scoped per content rating, with configurable inheritance; ADR-095 — age-conditioned policies and a seeded global minors policy; ADR-096 — policies may apply to input, and global ones screen incoming text by default; ADR-098 — policies may also apply to image prompts and image outputs, and image checks fail closed.
**Context:** Memory can be poisoned; refusals break immersion; naive loops can spin forever on poisoned memory.
**Decision:** Policies live in an immutable store evaluated on every message regardless of memory. Enforcement is an in-character deflection transform. Quarantine/sanitize + a bounded retry budget + safe fallback break poisoning loops. Policy never yields to memory or external content.
**Consequences:** Enables safe self-policing worlds (e.g., teen-appropriate) that resist poisoning; mutation primitives and a retry contract are required.

---

### ADR-012 — Directives and bitemporal retcon under an authority hierarchy
**Status:** Accepted.
**Context:** Users edit established facts; silent overwrite corrupts dependent facts.
**Decision:** Author/GM > narrative canon > in-world assertion (external authority rules its own domain). Directives are classified ephemeral vs. durable; durable fact changes cascade through the dependency neighborhood, auto-fix incidental mentions, flag cascaded justifications, and surface irreducible conflicts to the author; the change is recorded bitemporally and is reversible.
**Consequences:** Retcon becomes authorial collaboration; reuses `verify` and `fact_neighborhood`.

---

### ADR-013 — Orchestrator extensibility; external data as a first-class fact authority
**Status:** Accepted.
**Amended by:** ADR-030 — external values are owned values that decide objective truth, not character knowledge.
**Context:** Real games own authoritative state outside Epistrel (balance, weather, clock, flags).
**Decision:** The consumer registers external-data providers (tools/Skills/MCP). The Orchestrator reads them live during generation and routes domain predicates to them during verification. Volatile external values are fixed on commitment via `external@timestamp` facts (recording/single-source-of-truth refined in ADR-020). External results are untrusted content, tiered by declared authority. (Refined in ADR-030: an authority governs objective truth, not character knowledge; reads are pinned per turn; providers are read-only by default; internal story States cover most games.)
**Consequences:** Extends Epistrel beyond open roleplay to real games; adds the external-provider registry, snapshot-on-commit, and an injection-surface concern.

---

### ADR-014 — Multi-character single-response composition
**Status:** Accepted.
**Amended by:** ADR-031 — interaction modes (sequential/simultaneous), roles, and reactive narration; ADR-047/059 — output is filtered for an explicit viewpoint.
**Context:** A request may ask several characters to respond/react together; naive handling leaks private thoughts and mishandles concurrent commitments.
**Decision:** Each responding character is rendered against their own viewpoint and verified against their own beliefs/authorities; concurrent `latent`/commitment effects are reconciled against one shared timeline; the results compose into one returned message.
**Consequences:** Directly addresses contradictory-concurrent-commitment; requires the MULTI-ISOLATION invariant and the S-DUET scenario. (Refined in ADR-031: interaction modes, participation roles, and adjudication.)

---

### ADR-015 — Model-agnostic via OpenAI-compatible interface; Apache-2.0 license
**Status:** Accepted.
**Context:** Maximize adoption and ecosystem reach for an OSS engine.
**Decision:** Target the OpenAI-compatible wire format (public or local models) for all model purposes and embeddings; license Epistrel under Apache-2.0 with a license-compatible dependency graph.
**Consequences:** Any public/local model works; permissive licensing invites embedding (including by commercial apps), which drives contribution. Avoid AGPL code dependencies.

---

### ADR-016 — API surfaces: REST + MCP + library; Story as the top-level container
**Status:** Accepted.
**Amended by:** ADR-092 — there is no MCP server; Epistrel is only an MCP client of external-data providers.
**Context:** Adopters differ (HTTP services, tool-using agents, in-process).
**Decision:** Expose the same core logic as a REST API (primary), a stateless MCP server (identifiers as arguments) *[superseded by ADR-092: no MCP server]*, and an in-process library. The top-level isolated instance is a **Story** (StoryID); a **Character** (CharacterID) is the viewpoint-bearing entity.
**Consequences:** Consistent terminology across docs; MCP tool content treated as an injection surface.

---

### ADR-017 — Deterministic-first job disposition; the model is reached only through measured, tunable gates
**Status:** Accepted. Resolves the split left open by ADR-006.
**Amended by:** ADR-065 — the extraction bullet is superseded: claim detection runs on every render by default; ADR-080 — per-turn call budget and deadline with ordered degradation; ADR-084 — gates and modules are set per story by its engine profile.
**Context:** For each internal job: does it need a model, is it on the hot path, and — for hot-path model use — what gate triggers escalation from the cheap deterministic attempt to the expensive model call? The entailment-fallback gate is the knob that most affects cost, latency, and the risk of false `supported` verdicts (silent canon corruption). The risks are asymmetric: a wrong deterministic `supported` corrupts the story; a needless escalation merely costs a call. So gates bias toward escalation.
**Decision:** Deterministic is the default and the hot path; a model is reached only through explicit gates that fail toward safety. Per-job disposition:
- **Verification / entailment (hot path, gated).** Engine Core normalizes claims and returns supported/contradicted/novel deterministically; it returns `uncertain` — and only then does the Orchestrator call the entailment model — on (a) an unresolved predicate, (b) a world-knowledge-dependent conflict, (c) a partial/ambiguous match. The gate is a **tunable predicate-resolution-confidence threshold**, shipped as config and launched conservatively.
- **Salience (hot-ish; model behind a deterministic pre-filter).** Rules skip obvious non-salient turns; the model resolves the rest. Biased toward recall — a missed signal (under-commitment) is the worse error.
- **Extraction backstop (hot path).** *Refined by ADR-065:* a structurally valid render does not prove every claim in its prose was declared, so claim detection now runs deterministic detectors on every render and the model extraction on every render by default (`extraction_mode = always`), with gating available only as an evidence-driven reduction.
- **Consolidation / reflection, importance scoring (off hot path, no gate).** Model-first, latency-tolerant; model tier chosen freely.
- **Everything else (deterministic, no model, ever).** Commitment lifecycle, visibility scoping, bitemporal reads, projections, repair-ladder selection, mutation primitives, directive-classification mechanics.
Launch conservative (high continuity, more escalation), then **tighten data-drivenly:** gates are tunable config, never hardcoded; every tightening is validated in **shadow/canary** before promotion; a predicate-shape graduates to deterministic only when its escalation **yield** (the fraction of escalations where the model flips the deterministic tentative verdict) is low **and** the residual flips are low-consequence. A model fallback's verdict is validated, never trusted: `uncertain`/low-confidence resolves to the safe disposition (needs-repair or surface to author), never a silent `supported` (cf. FR-MODEL-6). Testability is unaffected — the cognition-port stub keeps the engine deterministic in tests regardless of which jobs use a model.
**Consequences:** Minimal hot-path model use with a safety bias; cost is driven down by evidence rather than guesswork. Requires the escalation instrumentation in FR-OBS-2, the yield metric in NFR-PERF-5, and shadow-evaluation test coverage. Threshold *values* are deliberately not fixed here — only the mechanism.

---

### ADR-018 — In-story time travel via sequence invalidation (truncate / regenerate)
**Status:** Accepted; **mechanism superseded by ADR-027** (truncation now uses timeline membership keyed by narrative position, not closing valid-time; "as of N" means the current corrected history through N). The decision to make rewind/regenerate one non-destructive, reversible primitive stands.
**Amended by:** ADR-027 — truncation is by narrative position and membership, not sequence number; ADR-072 — checkpoints distinguish `N` from inter-turn slots `N⁺k`; ADR-122 — compaction is an explicit, irreversible control operation; undo, swap, and promotion across it return `compacted`; ADR-028 — a message is identified by its turn operation, not a `seq` range
**Context:** Players delete-and-rewind (deleting a message removes everything after it) and regenerate the latest message. Both must reconstruct prior state without corrupting history, and remain undoable.
**Decision:** One in-story primitive — *reconstruct story state as of sequence N *[superseded by ADR-027: state is reconstructed by narrative position, not `seq`]** — via bitemporal validity invalidation. The event `seq` is the single linear spine; a message is a contiguous `seq` range *[superseded by ADR-028: a message is identified by its turn operation]*; every derived record carries its `source_event`. **Truncate** closes valid-time for events `seq` > N *[superseded by ADR-027: truncation discards by timeline membership and never alters valid-time]* (never hard-deletes, so it is reversible); **regenerate** truncates to the last checkpoint and replays (FR-COMMIT-8). Derived memory is valid-as-of-N iff all its source events are, and is rebuilt by re-consolidation at the new head. Every external read is recorded as an event for deterministic within-story replay. (Fork and whole-story deletion are story-level operations — see ADR-019.)
**Consequences:** Rewind and regenerate share one mechanism; in-story deletions are reversible until the consumer explicitly compacts them, after which undo, swap, or promotion that needs compacted records returns `compacted`; the audit trail survives. Invalidated in-story events accumulate unless compacted (FR-STORE-19, FR-STORE-32). Supersedes the naive "associate each memory with a message and delete it" approach, which would break event sourcing and undo. *[revised per ADR-122]*

### ADR-019 — Stories are self-contained; fork is a full copy; deletion is a scoped delete
**Status:** Accepted. **Supersedes** the copy-on-write / shared-ancestry direction once considered.
**Amended by:** ADR-027 — fork copies the corrected history through N (amendments applied); ADR-072 — fork's default boundary is `N`, excluding inter-turn slots; ADR-128 — stories export as versioned bundles and import only as new stories.
**Context:** Two requirements decide this independently: (1) deleting a story must actually and cleanly remove all its data — users create and discard stories constantly; (2) a fork must be able to rewrite its *own* history without affecting the original. Shared ancestry makes deletion a reference-counting/GC problem and makes per-fork history rewriting incoherent. Fork cost was the only argument for sharing, and a full copy of one hobby-scale story is a bounded Postgres `INSERT … SELECT` — the same order of work existing roleplay engines already do when copying messages/memory.
**Decision:** A story is a **fully self-contained dataset** (no cross-story references). **Fork/duplicate** = a full independent copy of the source story's *current corrected history through N* (ADR-027) under a new `story_id`, isolated at creation, with no propagation either way. **Delete** = a `story_id`-scoped delete with no reference-counting and no cross-story effect. The fork-after-rewrite ordering case is handled by the corrected-history view: amendments carry no narrative position, so one recorded after N still applies to a fork at N when it corrects pre-N history.
**Consequences:** Trivial, correct deletion and RTBF (scoped, no fork-tree reach-down); independent per-story rewriting; forks are time snapshots (later edits never cross). Slightly more data copied per fork than a pointer would be — accepted, and revisitable only if a pathologically large story ever makes a copy slow. Does not preclude adding sharing later; choosing sharing now would have precluded clean deletion/rewrite.

### ADR-020 — Every external read is an event; single source of truth for external values
**Status:** Accepted.
**Amended by:** ADR-030 — reads are of owned values; ADR-056 — a confirmed effect creates a post-effect value version within the turn.
**Context:** External-authority values (balance, weather, clock) must be deterministically replayable for within-story reconstruction/regeneration/audit, and the earlier "live-read to generate, independent snapshot-on-commit" model risked two drifting records of one reading.
**Decision:** Every provider call is recorded as an `external_read` event (provider, query, value, timestamp). Reconstruction replays recorded reads, never issues live calls. A commit that depends on an external value *references* its `external_read` event rather than re-capturing it — the event is the single source of truth.
**Consequences:** Deterministic replay; a free audit of "what the game reported at turn T"; one record per reading. Recording adds event volume (acceptable at hobby scale; compaction available).

### ADR-021 — Natural-language history rewrite with configurable, orthogonal modes
**Status:** Accepted.
**Amended by:** ADR-027 — amendments have no narrative position and survive truncation; ADR-042 — undo is a checked amendment; ADR-053 — an author's thunk resolution still passes the reconciliation check.
**Context:** The structured store is not hand-editable, so history rewriting must be author-facing and natural-language, built on FR-DIRECT + `verify`/`fact_neighborhood`. Adopters range from fast consumer apps to continuity-obsessed authoring tools, so disposition and preview must be consumer choices.
**Decision:** A natural-language history side-channel per story. Two orthogonal consumer-set parameters over a shared pipeline (detection identical; mode governs disposition; preview is an early exit before commit): `resolution_mode` ∈ {`synchronous`, `best_effort`, `threshold(severity)`} and `preview` ∈ {`preview_first`, `apply_directly`}, both defaulting to the safe end. Every rewrite is one atomic, reversible bitemporal transaction within a single story, in all modes — recorded as an *amendment* with no narrative position (ADR-027). Expresses the standing principle: where safety trades against smoothness, expose the policy and let the consumer choose.
**Consequences:** One engine, not forked code paths; safe defaults; authoring-grade continuity available; best-effort speed available. Requires atomic+reversible rewrite transactions as a hard invariant.

### ADR-022 — Determined/undetermined (world) and known/unknown (viewpoint) are separate axes; a determined fact has one value
**Status:** Accepted. Corrects the per-knower wording once in FR-COMMIT-6.
**Amended by:** ADR-049 — "one object per subject" is superseded by one object per declared **uniqueness key**; ADR-053 — an undetermined fact's resolution is not consequence-free; ADR-035 — contradiction is defined by deterministic rules on normalized propositions
**Context:** The spec conflated two axes: whether the world has fixed a truth (global) and whether a character holds it (per-character). "Undetermined for another character" wrongly implied a character carries an independently resolvable value for a fact the world already fixed.
**Decision:** *Determined/undetermined* is a global world-state axis; *known/unknown* is a per-character viewpoint axis. A determined proposition has exactly one truth value at a given valid-time (for a functional predicate, one object per subject per time *[superseded by ADR-049: one object per declared uniqueness key per time]*; refined in ADR-035); characters differ only in partition presence. A thunk is global and resolves exactly once. A character's divergent view is a separate owned belief record, never a second value. Any character's `think`/`do` may resolve a genuine (global) thunk, once; resolution is optional (unforced thunks may never resolve); a late resolution is weight-biased toward insignificance and toward entities with no retroactive epistemic obligations, with a could-have-known soft flag that scales with the story-time gap; an author directive overrides that bias and needs no history rewrite (the fact was undetermined) *[superseded by ADR-053: the outcome is the author's, but the resolution passes the retroactive-obligation check, and conflicts proceed as an amendment with blast radius and preview (FR-COMMIT-12,14)]*.
**Consequences:** No per-character value for determined facts (enforced/flagged); clean separation of ignorance from alternative-truth; deferral becomes a signal of low stakes and preserves cheap user steering; prevents the model from inflating loose threads into reveals.

### ADR-023 — Character modeling: interaction-or-salience trigger; model liberally, weight conservatively; player has no interiority
**Status:** Accepted.
**Amended by:** ADR-047 — "never surfaced or hinted" is superseded by non-disclosure (verbatim, paraphrase, or narrated inner state) with legitimate inference from visible behavior; player output is viewpoint-filtered.
**Context:** Real stories accrue minor characters (a recurring delivery driver) who should have memory, but the model tends to inflate every minor character into a plot point; and the human player should not be modeled the same way as NPCs.
**Decision:** Modeling is triggered by **direct interaction** (dialogue, mutual acknowledgment) **or explicit salience** — not by mere appearance. Existence and narrative weight are separate axes: **model liberally, weight conservatively** — a modeled character carries no implied significance and stays low-weight unless the story develops it; minor-character modeling is lazy and sparse for cost. A modeled minor character is a **full character** with first-class private `think`/memory under the same non-leakage rules; "minor" limits weight, not privacy. Private `think` is never disclosed to the player (verbatim, paraphrased, or as narrated inner state); inference from the character's own visible behavior is legitimate (refined by ADR-047). Grounded resurfacing of a *recorded* private memory later is encouraged (the long-horizon payoff); inventing an unrecorded "memory" is confabulation, caught by verify (discriminator: a backing partition record). The **player** exists as a modeled character for others' memory/beliefs but has no engine-held interiority (`say`/`do` recorded as witnessed; no `think`, no partition; asymmetry machinery does not run for the player-character; the human is authoritative over their own utterances).
**Consequences:** Minor characters accrete into recurring ones with real memory; the engine restrains narrative inflation without suppressing grounded callbacks; the player's interiority stays where it belongs (the human's head); sparse modeling controls cost at hobby scale.

### ADR-024 — Declared `think` is checked, not trusted; false beliefs require a permitted, antecedent-backed cause
**Status:** Accepted.
**Amended by:** ADR-034 — antecedents must be independently verified and earlier (staged beats/steps qualify); ADR-036 — lies are classified by the three speech dimensions.
**Context:** Treating `think` as a self-authenticating "intent oracle" is a loophole: one model can generate both an error ("I got my degree at Florida", vs canon undergraduate institution OSU) *and* the thought that launders it ("I remember attending Florida", or "I'll lie about Florida"). The discriminator for the lie/false-belief/hallucination trichotomy must be independent of the thing it judges.
**Decision:** `think` explains an action against *prior* belief state; it cannot create belief state in the same breath. A character's belief set is engine-owned canon about that character. A canon-contradicting claim is accepted as a false belief only if (a) the belief already exists in the character's partition with provenance, or (b) a **permitted belief transition** is backed by an antecedent the engine already holds, dated before invocation — permitted causes being misinformation, misperception, could-have-known gap-fill, a standing impairment trait, or author direction (FR-EPI-7/13). Resolution is **citation-with-engine-fallback**: the render should cite the antecedent; the engine confirms it, or searches history if none is cited; absent any antecedent, the claim is a hallucination → repair. A declared lie requires a legitimately formed underlying belief to deviate from (refined in ADR-036: that belief need not be objectively true). Belief provenance (FR-EPI-7) is promoted to mandatory to make this enforceable.
**Consequences:** The model cannot bootstrap a false belief and its justification in one step. (Refined in ADR-034: the boundary is an independently verified antecedent available before the dependent step, not a prior turn — verified earlier beats and earlier steps qualify.) The residual multi-turn case — a model plants a misinforming event in an earlier turn, then "believes" it later — is **correct by design**, not a leak: the belief then has a real, auditable in-story cause. The guard draws the line at antecedent-on-the-record vs. self-attested-in-the-moment, which is the right line. Requires belief state in the generation context (FR-TURN-8) so the model works from supplied beliefs rather than inventing them.

### ADR-025 — Belief formation is explicit, typed, and resolved as-of the exposure frame
**Status:** Accepted.
**Amended by:** ADR-027 — positions use the three coordinates; ADR-034 — antecedent rule; ADR-041 — lazy resolutions are story truth with two anchors; ADR-069 — nested beliefs are explicit records; ADR-075 — exposure positions may be inter-turn slots `N⁺k`.
**Context:** The spec defined belief storage and legitimacy but not *formation* — the transition from a public event to a specific character's belief. Recording that everyone heard Jenni's claim does not establish what anyone believes; and resolving a witness's belief lazily risks polluting a past decision with later-known evidence.
**Decision:** A public event records **exposure / a speech act, not belief**. Belief is a separate per-character transition via typed operations — observation, testimony, inference, discovery, correction, forgetting, revision — each with dated provenance recording which operation produced it. Testimony is never auto-accepted; acceptance/rejection/suspension is a trust-gated transition that may yield a first-order belief, disbelief, or a second-order "believes X was claimed." Confidence is **dual** (categorical always; optional scalar as source-of-truth with the category as its projection). Formation is **lazy and salience-proportionate**: non-salient witnesses get an exposure record (proposition, source, exposure position); belief is resolved on first need. **Lazy resolution resolves as-of the exposure's narrative position** — using the current corrected history through that position (ADR-027) for the believer's partition and canon, then folding forward later dated transitions — so narrative evidence from after the exposure can never pollute it, while amendments correcting earlier history still apply; deferral saves computation, not information (same bitemporal principle as the commitment checkpoint and recorded external reads, ADR-018/020). Once resolved, a belief is **logged as a dated event and established** (durable, provenance-stamped), not recomputed; retcon invalidates/cascades it like other derived state. Later legitimate evidence reaching the character produces a new dated transition (e.g., correction), distinct from the exposure resolution.
**Consequences:** Could-have-known (FR-EPI-4) becomes mechanically enforced via acquisition-record replay; retcon repair walks beliefs by acquisition provenance; the acquisition types and the permitted causes (ADR-024) are one list. The anti-pollution guarantee and the lazy fan-out bound are the two requirements that make per-character belief tractable at scale. (Refined in ADR-034: the exposure frame records turn, beat, and step.)

### ADR-026 — Presence is not perception; route by an authoritative audience, with an envelope/content split
**Status:** Accepted.
**Amended by:** ADR-043 — widening qualifiers (eavesdrop, remote) must have a plausible perception path; ADR-047 — player output uses the same routing; ADR-071 — character working memory is filtered the same way.
**Context:** Routing `say`/`do` to "everyone present" over-grants knowledge (whispers, closed doors, foreign languages, remote channels) and — worse — let the *non-authoritative* scene roster control who gains memories, a privilege-escalation path from untrusted input.
**Decision:** Presence establishes only a *candidate* audience; an **authoritative** perception resolution establishes who actually perceived what, and memory writes key off that, never the roster. An utterance splits into an **observable envelope** (speech occurred, speaker, apparent target, visible manner/contact/reaction, situating context) and a **content payload** (the words): the envelope routes as a full-fidelity observation to all authoritatively co-located characters; the content routes by audience (co-located default, narrowed by explicit whisper/aside, widened by explicit eavesdrop/remote). Resolution is **deterministic and deliberately dumb** — same location → perceive, different → not, deviations only when the render explicitly makes them; perception is **not** model-adjudicated (no inferred distraction/acuity). Content fidelity is whatever the render established (full by default; partial only if explicitly limited). The witnessed envelope is an observation that may seed low-confidence inference but grants **no acquisition path to withheld content** (FR-BELIEF-11). Routing derives from authoritative locations/barriers/channels; the roster may inform candidates but never grant perception.
**Consequences:** A character can notice and react to a whisper without learning its words; remote comms fall out as an audience the channel defines; the untrusted roster cannot grant private knowledge (NFR-PRIV-3). Explicitly *not* a perception simulator — the "don't get too cute" decision — so a future contributor does not reintroduce model-adjudicated perception.

### ADR-027 — Three coordinates (narrative position, valid-time, transaction-time), explicit timeline membership, and amendments
**Status:** Accepted. Supersedes the truncation mechanism in ADR-018; refines ADR-003, ADR-019, ADR-021, and ADR-025.
**Amended by:** ADR-028 — operations, categories, continuations; ADR-042 — corrections attach to propositions or records; ADR-060 — the world clock is the source of valid-time; ADR-072 — inter-turn slots `N⁺k` refine narrative position between turns; ADR-076 — as-originally-recorded views are defined at both `N` and `N⁺k` commit boundaries; ADR-079 — amendment eligibility is per correction part, compared in clock offsets where possible.
**Context:** The spec defined "as of N" as folding events through sequence N and implemented truncation by closing valid-time. Both conflate coordinates. Play to 1,000, retcon Bobby's childhood at 1,001, fork at 800: folding through 800 drops the retcon, contradicting the requirement that the fork carry the corrected history. And closing valid-time cannot abandon a discarded message that describes something earlier in story-time (a flashback at message 900 about age seven).
**Decision:** Keep three coordinates with one job each, never substituted: **narrative position** (checkpoints, truncation, regeneration, fork, membership — originally defined here as the `seq` of the introducing message; corrected in ADR-028 to turn index plus variant), **valid-time** (story-time — retcons, flashbacks), **transaction-time** (record-time — audit, undo). Every record carries an **active/discarded membership** flag stamped with transaction-time; truncation flips membership for records introduced after N and never touches valid-time. "As of N" for every player-facing operation means the **current corrected history through N**: active records with narrative position ≤ N, latest transaction-time versions, all applicable amendments in force. A separate **as-originally-recorded** view (transaction-time capped at message N) exists for audit only. Side-channel history rewrites are **amendments**: transactions with no narrative position; one applies to a checkpoint view when it corrects a record at or before N or introduces a fact whose valid-time begins at or before the checkpoint's story-time. Amendments **survive truncation** and are undone separately; a directed regeneration is an ordinary narrative event and does not. When a fork or truncation keeps an amendment whose rationale depended on excluded records, the rewrite conflict check surfaces it.
**Consequences:** Retcon-then-fork yields the corrected history; flashback truncation works; deleting later messages never silently undoes a deliberate history edit; lazy belief resolution (ADR-025) is pinned to narrative position under current corrections, so later narrative can't pollute it but retcons still apply. Adds a membership field to every record and an `amendments` table; the as-originally-recorded view is retained for debugging.

### ADR-028 — Event model: turn index ≠ seq; operations and categories; continuations and swap-on-undo
**Status:** Accepted. Corrects ADR-027's definition of narrative position.
**Amended by:** ADR-041 — lazy belief resolutions; ADR-072 — inter-turn operations (State sets, settlements) occupy slots `N⁺k` instead of "the current head"; ADR-086 — backfill is its own operation, pausing the story while it re-derives from the transcript
**Context:** ADR-027 defined narrative position as the `seq` of the introducing message and treated a message as a contiguous `seq` range. But consolidation, rejected renders, external reads, rewrite discussions, and control actions also produce events that interleave with turns. A reflection computed between turns belongs to neither; a rejected draft is not story content. Separately, undoing a rewind after new turns had been written had no defined behavior.
**Decision:** `seq` is global recording order only. **Narrative position is the turn index plus variant**, advanced only by narrative turns. Every event carries an **operation ID** (turn, consolidation, rewrite session, control, maintenance) and a **category** (narrative state, input, derived, amendment, audit, control); categories govern view inclusion, and audit records are never read by story views. Records follow their operation's membership. **Derived records are judged by provenance in the corrected-history view and by creation time in the as-originally-recorded view.** Derivation jobs declare a **source window**, read only those inputs, and drop their output if any input is discarded before commit. Each turn has a **variant ID**; exactly one variant per position is active and the active variants form one chain. **Undoing a rewind** reactivates when nothing new was written; otherwise it defaults to a **swap** (original continuation active, newer one dormant, itself reversible), with the option to promote the dormant continuation into its own story by fork. Undo never rejects or destructively overwrites. **Resolutions follow the membership of the operation that produced them**, so each continuation keeps its own; amendments survive a swap and are re-checked against the restored chain.
**Consequences:** Narrative position is stable regardless of how much background or audit activity occurs; consolidation can't commit against a timeline the user just abandoned; nothing a player writes is lost to an undo. Adds an `operations` table, category and variant fields, and source-window tracking on derived records.

### ADR-029 — Optimistic concurrency, per-story turn policy, and idempotent mutations
**Status:** Accepted.
**Amended by:** ADR-040 — read sets are query keys; ADR-045 — idempotency results are kept for a window, then a permanent tombstone; ADR-052 — configuration revisions are captured and revalidated.
**Context:** Atomic commits guarantee a turn lands all-or-nothing but do not stop two requests from generating against the same stale state. Generation spans seconds and multiple model calls, so locks cannot be held across it. Nondeterministic generation also makes unkeyed retries dangerous (a retry yields a different message). Rewrite previews and background jobs can likewise act on a story that changed underneath them. And the right same-story policy differs by use: roleplay is strictly one-at-a-time, while games may run several scenes at once.
**Decision:** Each story has a **timeline revision** bumped only by committed changes to story truth (not by derived, maintenance, or audit work). Operations record a base revision and a **read set**; model calls hold no locks; the commit transaction locks the story row briefly and revalidates — commit if unchanged, **rebase** (re-verify) if the read set is untouched, **regenerate** within a budget if it was touched, **abandon** if a control action intervened. **Same-story turn concurrency is configured per story** (`turn_concurrency` ∈ {`reject`, `queue`}, default `reject`): roleplay rejects overlapping turn requests as busy; games queue them FIFO with a depth limit, each generating against the post-commit state. Different stories always run in parallel; non-turn operations commit through the revision check. Every mutating request takes an **idempotency key** (stored result returned on retry *[revised per ADR-045: within the result window; afterwards, a reference to the original operation]*; mismatched payload rejected; in-flight duplicates wait). Rewrite previews carry a **base revision** and are revalidated against their blast radius at final commit, re-previewing if stale. Background jobs commit only if every input is still active and unchanged, otherwise drop and recompute. Forks read a snapshot pinned to one revision.
**Consequences:** No lost or duplicated turns; retries are safe; approved rewrite plans can't land on a changed story; background work can't commit against a rewritten timeline and never blocks player turns. Adds `timeline_revision`, read-set tracking, an idempotency-key table, a per-story queue, and per-story concurrency settings. Expresses the standing principle again: where use cases genuinely differ, the behavior is a per-story setting with a safe default.

### ADR-030 — Story States and owned values; authorities decide objective truth, not knowledge
**Status:** Accepted. Refines ADR-013 and ADR-020.
**Amended by:** ADR-037/038/039 — outage semantics, effect lifecycle, effect authorization; ADR-060 — the world clock is a built-in State.
**Context:** The external-authority design let a live balance override a character's claim, conflating objective truth with character knowledge. It also left open which value governs a turn when a provider changes mid-turn, what happens when a provider is down, whether author directives can override provider-owned values, and whether "I spend 50 gold" can change the game's balance — which Epistrel's transaction cannot make atomic. Separately, most games only need a handful of tracked values, and pushing them into an external store creates sync problems and breaks time travel (rewinding Epistrel can't refund a game server's gold).
**Decision:** Introduce **States**: typed single-value variables (number, boolean, enum, short text) scoped to the story or a character, with constraints, visibility, and an author/consumer-defined schema (the engine may propose new States, never create them). Every tracked value has an **owner** — `story` or `external` — sharing one declaration and verification model. State changes are **declared** in a new state-effects channel of the turn contract, validated against constraints, and committed with the turn; they are events, so rewind, regenerate, fork, swap, and amendments apply automatically. **Player actions are attempts**: a constraint violation changes nothing and is repaired in character. Rules are limited to constraints and simple deterministic triggers — no scripting language. For both owners, the value is **objective truth, not character knowledge**: objective claims verify against the owner; first-person claims verify against the character's beliefs through the lie / false-belief / hallucination test. External reads are **pinned per turn**; each provider declares an **unavailability policy** (fail, last-known-if-fresh, or unknown — where unknown blocks only new objective assertions and newly acquired exact knowledge, never existing beliefs or lies; refined in ADR-037); author directives **cannot override** externally owned values; providers are **read-only by default** with implied changes returned as state effects, and write-capable providers use a **transactional outbox** with idempotent dispatch and a reconciliation status — never claimed atomic.
**Consequences:** Most games need no external store and get time travel over their game values for free; characters can sincerely be wrong or lie about money without being "corrected" by the ledger; external integrations have explicit, honest consistency semantics. Adds State definitions, change events, proposals, an outbox, and a state-effects channel to the turn contract and respond response.

### ADR-031 — Multi-character interaction modes, participation roles, and adjudication
**Status:** Accepted. Refines ADR-014.
**Amended by:** ADR-047/059 — composed output and beats are filtered for the output viewpoint; ADR-126 — a fixed role is a bound; escalation that cannot happen is deferred and reported.
**Context:** Independent per-character generation cannot let one character respond to another within the same message, and gives no rule for shared physical actions (Bobby takes the key while Jenni locks it away). Forcing a full render for every present character is also wasteful: five characters shouldn't mean five renders to show a handful of reactions.
**Decision:** A multi-character turn runs in an **interaction mode**, set per story and overridable per request: **sequential** (default) — ordered micro-events, each primary character rendering with the perceived output of earlier beats, so later characters can respond and physical conflicts resolve by causality; order defaults to request order with an optional director-chosen order; each beat is verified before the next renders. **Simultaneous** — all primaries render against the same base without seeing each other; conflicts on the same object or State are detected deterministically and settled by a per-story tie-break (request order by default, or an initiative State, or director judgment), with the losing action repaired as an in-character attempt. Each character has a **participation role** — **primary** (full render), **reactive** (visible reactions narrated within another render), or **silent** — defaulting per story, fixable per character, overridable per request. Reactive narration is attributed to the reactor as an observable action only, never writes their thoughts, hidden facts, beliefs, or States, must pass their perception and could-have-known checks, and is non-verbal by default (brief interjections are a per-story option). A reaction needing real speech, a decision, a hidden fact, or a State change escalates the character to a primary render *[revised per ADR-126: unless the character's role is fixed or the budget cannot fit it; the reaction then stays within reactive bounds and the rest is deferred]*. All beats commit as one turn with a beat index.
**Consequences:** Characters can genuinely interact within one message; shared actions have a defined winner; crowded scenes stay affordable because most characters can be reactive; reactive characters keep their memory of the scene through exposure records. Sequential mode serializes primary renders, so reactive roles are the main latency lever.

### ADR-032 — Closing verification and input gaps: verified composition, explicit input contract, novelty permissions, typed dependencies
**Status:** Accepted.
**Amended by:** ADR-036 — speech dimensions; ADR-043 — reactive narration is a named novelty exception; ADR-047/059 — composition checks are viewpoint-specific; ADR-065 — composition is re-run through full claim detection; ADR-087 — output claims are precise; Lite does no structured claim checking
**Context:** Four holes remained in the verification and input path. The final polish step could add claims, swap speakers, or leak thoughts after every beat had passed verification. Player moves had no authoritative entry point (recent messages are non-authoritative) and repeated ingestion had no deduplication. A `novel` verdict effectively authorized any invention, including another character's secrets, player actions, or game-owned values. And retcon cascades treated every dependency alike, so retconning a university risked rewriting an intentional lie about it.
**Decision:** (1) **Composition is constrained and verified**: it may only order and join verified beats and add connective prose, then passes a final claim/attribution/privacy check, falling back to deterministic assembly on failure. (2) **Explicit input contract**: a player-input field enters history as the turn's first beat; an ingest operation records events without generating, deduplicated by client event ID; `player_authority` is per story (`own_character` default — input about other characters is a request the world resolves; `co_author` — players may narrate other characters' observable actions, viewpoint-checked, never their thoughts). (3) **Novelty requires permission** by asserter and subject owner; unpermitted novelty is repaired, not canonized. (4) **Typed dependency edges** — `causes`, `presupposes`, `derives`, `mentions`, `asserts` — with per-type cascade rules; speech acts stay as history and only their objective relation (and, if the speaker's belief changed, their belief relation) is re-evaluated — never their intent (ADR-036); testimony-acquired beliefs stay, observation-acquired beliefs cascade.
**Consequences:** No unverified text reaches the player *[revised per ADR-065 and ADR-087: every detected claim is checked, under profiles that verify; Lite does no structured checking]*; player moves have one authoritative, idempotent path; the model can't use novelty to author things it doesn't own; retcons preserve what characters actually said and believed.

### ADR-033 — Consolidation scope, authorization, erasure boundary, and measurable acceptance
**Status:** Accepted.
**Amended by:** ADR-044 — the eval gate rule ("mean clears floor by 1 SD") is superseded for zero-tolerance metrics; ADR-045 — re-embedded vectors are not byte-identical; ADR-046 — erasure is a control action with a restore-surviving ledger; ADR-070/072 — vector retention follows the hot/retired tier; ADR-071 — consolidation preserves epistemic type; ADR-091 — Epistrel authorizes no end users; it authenticates the calling service and guarantees privacy per requested viewpoint; ADR-103 — erasure reaches images only through declared or named subjects; ADR-078 — request roles are separate request fields
**Context:** Consolidation could merge private memories into a reflection more visible than its inputs, and did not distinguish replaying a stored result from rerunning a model. Story isolation was being relied on as if it were authorization. Erasure was described as scoped row deletion, though subject references survive in prose, summaries, rejected outputs, logs, caches, exports, and backups. Acceptance criteria mapped only requirement areas to tests, eval floors were unspecified, "any compatible model works" conflated protocol with capability, and the soak test expected sublinear storage from an append-only log.
**Decision:** **Consolidation** preserves scope (output visibility = intersection of input scopes; objective reflections only from public records) and records model/version/prompt hash; replay reuses stored results, reruns are new operations. **Authorization** uses caller roles — player, author/GM, operator, internal service — granted per story and enforced on every surface; players can never reach NPC private state. *[superseded by ADR-091: Epistrel authenticates only the calling service, authorizes no users, and guarantees privacy per requested viewpoint]* **Erasure** has a stated boundary: structured deletion, subject-reference indexing so derived prose can be redacted or regenerated, rebuilt caches, an erasure ledger reapplied after restore, no story content in operational logs by default, and explicit exclusion of delivered exports and provider-held data. **Acceptance** is per requirement: every test has an ID and a generated appendix maps every Must requirement to at least one test, checked in CI; numeric **NFR-QUAL** floors are set (0 asymmetry violations and leaks; entailment false-contradiction ≤ 2% and false-support ≤ 1%; consolidation faithfulness ≥ 98%; salience recall ≥ 95% / precision ≥ 70%; structured output ≥ 99%; resolution recall@10 ≥ 0.9; generation conformance ≥ 98%). **Protocol compatibility** is guaranteed separately from per-purpose **capability**. **Storage** is stated honestly: the active working set stays bounded *[revised per ADR-070: the hot retrieval sets are bounded; history is not]* and total storage is linear with a bounded constant under the retention policy (dormant continuations kept until story deletion, compaction opt-in; audit records 90 days by default).
**Consequences:** Consolidation can't leak; API access matches story roles *[superseded by ADR-091]*; erasure promises are ones the engine can keep; acceptance is measurable and enforced per requirement.

### ADR-034 — Belief antecedents: independently verified and earlier, not "a prior turn"
**Status:** Accepted. Refines ADR-024 and ADR-025.
**Context:** FR-EPI-13 forbade a render from establishing a belief's cause and effect "in the same turn." Sequential multi-character turns need exactly that: Bobby says "The bridge collapsed," Jenni hears him, believes him, and replies "Then we'll take the ferry" — all inside one atomic turn. The real hazard was never same-turn causation; it was self-attestation. Lazy exposure frames also only recorded the turn, so a later beat in the same turn could influence an earlier character's belief.
**Decision:** A belief's antecedent qualifies when it is **independently verified and precedes the dependent step in causal order**: a record committed in an earlier turn, a **verified staged beat** earlier in the same turn (before database commit, sequential mode only), or a **verified earlier step** within the same render. Within one render, belief change is declared as an explicit **causal order of steps** (observe → believe → act), verified in order; an observation step must have an independently verifiable object. Narrative position is refined within a turn to **(turn, beat, step)**; exposure records store all three and lazy resolution reconstructs to that exact point. Simultaneous mode has no cross-character same-turn antecedents; an abandoned turn discards beliefs formed from its staged beats; a regenerated beat replaces its staged version before later beats render.
**Consequences:** Characters can genuinely hear, believe, and act within one message, while self-justifying beliefs remain impossible and later evidence — even a later beat in the same message — can never reach backward into an earlier belief.

### ADR-035 — Normalized propositions, a layered predicate registry, and deterministic contradiction rules
**Status:** Accepted. Refines ADR-006, ADR-010, and ADR-022.
**Amended by:** ADR-048 — support requires coverage, not overlap; ADR-049 — cardinality is per uniqueness key, education predicates are distinct, exhaustiveness only by closed sets; ADR-050 — weak predicates still contradict on direct polarity; ADR-058 — temporal relations are separated.
**Context:** Deterministic verification had no defined predicates to operate on, so "contradicted" was assumed rather than computed. The OSU/Florida example assumed a conflict that may not exist: someone can attend both institutions; only "the institutions that awarded a particular degree" is single-valued (refined by ADR-049). FR-EPI-8's "one global value" also read as forbidding multi-valued relationships.
**Decision:** Claims are normalized into viewpoint-free **propositions** (subject entity, predicate, object or literal, qualifiers, polarity, valid-time interval with exact or approximate bounds); holder, provenance, visibility, and confidence live on the wrapping record. **Predicates** are registered with argument types, cardinality (`functional` / `multi` with optional maximum), temporal kind (`permanent` / `fluent` / `event`), exclusions, containment, bounded implications, symmetry, and owner (States are functional predicates). Semantics come from three layers: a shipped **core vocabulary**, **author-declared** predicates, and **auto-registration with the weakest semantics** (`multi`, no exclusions or implications), which can never produce a false contradiction; stronger semantics require the core vocabulary or author approval. **Entities** have canonical IDs and aliases; ambiguous mentions are `uncertain`; identity (`same_as`) is itself a holder-scoped proposition, so identity knowledge can be asymmetric. The world is **open** (absence is not negation); explicit negations are stored. Deterministic **contradiction rules** cover polarity conflict, functional-object conflict over overlapping time (respecting containment), cardinality maximums, exclusions, and implication violations; non-overlapping intervals never conflict; undecidable cases return `uncertain` to the model residual. "One value" means one truth value per proposition per valid-time — one object per subject per time for functional predicates *[superseded by ADR-049: one object per declared uniqueness key per time]* — not a ban on multi-valued relationships.
**Consequences:** `verify` has a precise, testable semantics; false contradictions from loose relations are structurally impossible; the model is reserved for genuinely open questions. Requires a core vocabulary, a registry, a mention resolver, and range-indexed proposition storage.

### ADR-036 — Speech acts carry three independent dimensions: objective truth, speaker belief, and intent
**Status:** Accepted. Refines ADR-008, ADR-024, and ADR-032.
**Amended by:** ADR-063 — time claims are classified the same way, against the speaker's time belief.
**Context:** `truth_relation ∈ {truthful, lie, unknown}` merged three different facts into one label. It could not represent a sincere error, an attempted lie that happens to be objectively true, or a lie about something the speaker is wrong about. A retcon that made Bobby's deceptive "UCLA" objectively correct flipped the label to "truthful," erasing the fact that he meant to deceive and still believed OSU.
**Decision:** Every utterance carries three independent labels — **objective relation** (true / false / unresolved, against canon or the value's owner), **speaker belief relation** (consistent / inconsistent / unknown, against the speaker's beliefs at that position), and **communicative intent** (sincere / deceptive / joking / unspecified, from the checked `think`). Classification derives from the combination: sincere error, lie (deceptive and belief-inconsistent, whatever the objective relation), joke, or hallucination (no legitimate basis). A lie requires a legitimately formed underlying belief to deviate from, which need not be objectively true. Divergences record asserted-vs-believed on deceptive intent. Retcons re-evaluate only the objective relation; the belief relation changes only if the speaker's belief changes; intent changes only by explicit author directive. Hearers' testimony considers perceived intent (jokes are not accepted as sincere by default).
**Consequences:** Deception, error, and truth are tracked separately, so retcons can't launder a character's intent and characters can lie about things they're wrong about. Utterance records gain three fields in place of one.

### ADR-037 — Provider outages restrict new knowledge, not existing beliefs
**Status:** Accepted. Refines ADR-030.
**Context:** FR-EXT-7's `unknown` policy said no character may assert a specific value while a provider is unavailable. That contradicted viewpoint-aware speech (FR-EXT-3): if Bobby counted 500 gold earlier, an outage shouldn't stop him from saying "I think I have 500."
**Decision:** Under `unknown`, the restriction applies only to **new objective assertions** (narration stating the current value) and **newly acquired exact knowledge** (a character learning the exact value this turn). Existing beliefs and deliberate lies keep their normal verification semantics — checked against the speaker's beliefs — with the objective relation recorded as `unresolved`.
**Consequences:** Outages degrade honestly: nobody gains knowledge the system can't vouch for, and nobody loses knowledge they already had.

### ADR-038 — External effect lifecycle: membership-gated dispatch, no executable fork copies, irreversible confirmations, honest prose
**Status:** Accepted. Refines ADR-030.
**Amended by:** ADR-051 — the in-flight point is the committed claim, not the network hand-off; ADR-054/061/066/072 — staged turns, late settlement, terminal outcomes.
**Context:** The outbox introduced lifecycle hazards: a payment queued by a turn could be dispatched after the player rewound past it; a fork could copy a pending purchase and dispatch it again; internal rewind could appear to undo actions an external system already performed; and prose could announce a purchase's success because its pending record committed.
**Decision:** Outbox rows follow their originating operation's membership; the dispatcher re-checks membership at claim time in the same transaction and cancels discarded effects. Forks copy outbox history only as non-executable records and report source-pending effects. Confirmed (and in-flight) effects are irreversible by rewind, regeneration, swap, fork, or erasure; operations that discard them return `reconciliation_required` and nothing is auto-compensated; swaps never re-dispatch cancelled effects. Providers declare synchronous or asynchronous confirmation; prose may state an external outcome only once confirmed, otherwise it describes an attempt, and later outcomes are surfaced in the next turn.
**Consequences:** No ghost payments, no double purchases from forks, explicit reconciliation where internal and external history diverge, and narration that never claims more than the game has confirmed.

### ADR-039 — State effects need authorization, action preconditions, transfers, and two-way completeness
**Status:** Accepted. Refines ADR-030.
**Amended by:** ADR-055 — State effects in turns with external effects are unconditional, reservations, or conditional; ADR-144 — an amount above a cap is repaired to the cap; the author grants more later
**Context:** Constraints stopped impossible values but not implausible ones ("gold +100000, found treasure"). Funds were conflated with a completed sale. And only declared effects were validated, so narration could claim a purchase while omitting its effect.
**Decision:** Each State declares allowed sources, justifying event types per direction, and per-effect and per-turn caps; out-of-policy effects are unpermitted novelty, and world gains need director/world permission within caps (author approval above) *[revised per ADR-144: the turn repairs to the cap; the author grants more later by directive or State API]*. Action preconditions (counterparty consent, availability, location, ownership) are verified as propositions independent of State bounds. Inter-party movement is declared as a balanced transfer when both sides are tracked. The extraction backstop checks completeness both ways: narrated consequential changes need effects, and visible effects need narration unless marked silent.
**Consequences:** Models can't mint wealth or sales; ledgers match the story; validation order is source → justifying event → preconditions → transfer balance → bounds.

### ADR-040 — Read sets are query keys, including empty results
**Status:** Accepted. Refines ADR-029.
**Amended by:** ADR-052 — configuration revisions are part of what is revalidated.
**Context:** Recording read row IDs and versions catches changed rows but not facts that appear later: a turn that found no lock could open a door an amendment had just locked. Rewrite previews likewise missed dependents created after the preview.
**Decision:** Every deterministic lookup behind a verdict, precondition, or blocker is recorded as a query key (subject, predicate, object or wildcard, holder, time window) with its result, empty included. Commit treats any matching change since the base revision — additions included — as touching the read set, via a change index or by re-running the lookups. Semantic retrieval used only as context is not keyed. Rewrite apply re-runs the dependency search and treats newly found dependents as staleness.
**Consequences:** "Nothing was there" becomes a checkable dependency; concurrent amendments can't silently invalidate a turn's assumptions.

### ADR-041 — Lazy belief resolutions: story truth, with exposure and resolution anchors
**Status:** Accepted. Refines ADR-025 and ADR-028.
**Amended by:** ADR-075 — the exposure anchor may be an inter-turn slot.
**Context:** It was unclear whether a lazily resolved belief was derived work (no revision bump) or story truth, and it carried only one anchor. With only the exposure anchor, a resolution made at turn 850 would wrongly survive a swap to a continuation that never made it; with only the resolution anchor, the belief would appear to start at 850 instead of turn 10.
**Decision:** Resolution happens only inside a turn that needs it, is story truth committed with that turn (covered by its revision increment), and is never persisted by read-only inspection. Each resolution records its **exposure position** (turn, beat, step) and its **resolution operation** (turn, variant). Inclusion follows the resolution operation: a view through N includes it iff that operation is active and at or before N, and truncation before it, a swap away from it, or a fork before it leaves the exposure unresolved. Valid-from follows the exposure position.
**Consequences:** A resolution is established within the continuation that chose it and is re-decidable after rewinding past that choice — consistent with the commit boundary. This intentionally differs from derived memory, which is a cache over its sources rather than a behavioral choice.

### ADR-042 — Amendment undo is a checked operation; corrections attach to propositions or exact records
**Status:** Accepted. Refines ADR-021 and ADR-027.
**Amended by:** ADR-079 — retcon eligibility is per correction part, in clock offsets where possible
**Context:** "Revert the amending transaction" breaks once later work depends on the amendment: if amendment A establishes UCLA and amendment B adds a UCLA graduation, a blind revert of A leaves B contradicting canon. It was also unspecified where an amendment's corrections live when the turn variant they touched goes dormant.
**Decision:** Undo is a **new, checked amendment** with its own blast radius computed against current state — later amendments, turns, derived memory, resolved beliefs — handled through the normal resolution and preview modes, with cascade-undo, repair, or keep per dependent; `best_effort` cascade-undoes dependents that would become contradicted. Amendments record typed dependency edges, including on other amendments. The original is marked reverted, never deleted; redo is another checked operation. Corrections attach by kind: **world-proposition corrections** attach to the proposition key and apply in whichever continuation is active (re-checked after swaps); **record corrections** attach to the exact record ID and follow that record's membership. No automatic cross-variant "equivalent event" matching — the engine may suggest a port, applied only on author approval.
**Consequences:** Undo can't strand dependent edits; world truth stays continuation-independent while line-level fixes stay tied to the exact text they fixed; no silent misapplication from guessing that two model-generated variants are "the same."

---

### ADR-043 — Ledger obligations per proposition; reactive narration exempt from novelty; widening perception validated
**Status:** Accepted. Refines ADR-009, ADR-026, and ADR-032.
**Context:** Three small gaps. (1) Salience said "commit" without saying *what*: does committing "a knock occurred" satisfy the promissory ledger while identity and motive stay open? (2) Novelty permissions forbid a render from authoring another character's actions, which is exactly what reactive narration does. (3) "Explicitly eavesdropping" or "on the phone" widened perception with no check that it was physically possible.
**Decision:** (1) Ledger obligations are tracked **per proposition** (FR-COMMIT-13). A salient event commits its own proposition; sub-propositions (who, why) remain open thunks unless themselves made salient or held with intent by a modeled character. (2) Reactive narration under FR-MULTI-9 and `co_author` player input are named **exceptions** to FR-VERIFY-11; reactive narration remains bound by its own rules (perceived events only, no `think`/decisions). (3) **Widening** qualifiers require a plausible perception path in recorded positions/barriers or an established channel; otherwise the claim is repaired (FR-TURN-13). **Narrowing** qualifiers (whisper, foreign language) need no validation.
**Consequences:** Knock-style mysteries stay open without blocking turn close; multi-character scenes don't self-reject; eavesdropping can't be used to launder knowledge. Tests U-16, U-17, S-KNOCK, S-NOVELTY-PERMISSION, S-WHISPER.

---

### ADR-044 — Separate gate rules for rate floors and zero-tolerance floors
**Status:** Accepted. Refines ADR-033.
**Amended by:** ADR-077 — the guarantee boundary is stated as "the model proposes; the engine validates"
**Context:** "Mean clears the floor by more than one standard deviation" is undefined for a floor of zero: a mean cannot be below zero, and an observed zero says nothing about the true rate without a sample size.
**Decision:** Rate floors keep the mean − 1 SD rule. Zero-tolerance floors pass only on **observed zero across all seeds on ≥ 300 labeled cases**, reported with the one-sided 95% upper bound (rule of three, ≈ 3/n), never as "zero rate." Every eval report states sample size, seed count, and model/version (NFR-QUAL-8).
**Consequences:** The gate is mathematically coherent and honest about what was measured; growing the labeled set tightens the reported bound.

---

### ADR-045 — Replay determinism excludes live re-embedding; idempotency has a result window and permanent tombstone
**Status:** Accepted. Refines ADR-029 and ADR-033 (retention defaults).
**Context:** Retention drops dormant vectors and re-embeds on reactivation, but hosted or GPU embedding is not bit-stable, so "byte-identical" was overpromised. Idempotency rows "expire," which silently voided the same-result-on-retry guarantee and risked re-execution of a late retry.
**Decision:** NFR-DETERM-1 covers Engine Core state other than live-re-embedded vectors; per-story `retain_dormant_vectors` restores exact reproducibility at a storage cost (FR-STORE-30). Idempotency stores full results for a **result window** (default 24 hours); afterwards a **tombstone** `(key, request hash, operation ID)` persists for the life of the story, so a late retry returns a reference to the original operation and is **never re-executed** (FR-CONC-6, NFR-REL-4).
**Consequences:** Guarantees match what the system can actually deliver; at-most-once execution holds forever, identical-response replay holds within the window. Tests I-14, I-15, S-RETENTION.

---

### ADR-046 — Erasure is a control action, guarded at every write path, with a ledger that survives restores
**Status:** Accepted. Refines ADR-033.
**Amended by:** ADR-080 — recovery and audit treat erasure markers as expected gaps; ADR-128 — imports apply the erasure ledger before the story is served.
**Context:** Erased content could come back two ways: an in-flight generation, rewrite, consolidation job, or outbox dispatch that read the subject before erasure and writes after it; or restoration of a backup predating the erasure, if the ledger itself was in that backup.
**Decision:** Erasure bumps the timeline revision, so in-flight operations fail their revision check and abandon; every write path also checks the erasure ledger and refuses content referencing an erased subject (FR-ADMIN-9). The ledger is stored **outside** backed-up story data and **copied into** every backup; a restored story is not served until the ledger is reapplied (FR-ADMIN-7).
**Consequences:** No resurrection by race or by restore. Cost: one ledger lookup per write path (indexed by subject ID) and a small, separately managed ledger store. Test S-ERASURE-BOUNDARY resurrection variants.

---

### ADR-047 — Explicit output viewpoint with an audience filter; disclosure is distinct from inference
**Status:** Accepted. Refines ADR-023, ADR-026, and ADR-032.
**Amended by:** ADR-059 — authorized omniscient output may include labeled thoughts, and stored results are re-authorized on every read; ADR-071 — the same filter applies to character working memory; ADR-091 — `omniscient` is returned only when named explicitly; who may name it is the consumer's decision; ADR-078 — request roles are separate request fields; ADR-145 — the audience filter applies only with perception; with perception `none`, a character viewpoint receives every beat except `think` and `latent`
**Context:** "Players see what their characters see" was an intention, not an output contract. Composition checked for private thoughts but not for perception, and the returned beat list had no audience filter, so Jenni's whisper could be withheld from Bobby's memory yet appear verbatim in the response to a player playing Bobby. Separately, "never surfaced or hinted" was absolute: it could be read as forbidding a character's visible behavior from revealing anything about their mood.
**Decision:** Every response, history read, and replay is produced for an explicit **output viewpoint** (FR-RESP-12): a character (default the acting player's character) or `omniscient`. For a character viewpoint, the same perception routing that writes memory filters each beat to content, envelope, or nothing, and both the composed narrative and the returned beats are built from the filtered set; composition is checked against it (FR-RESP-8). *[revised per ADR-145: this audience filter (FR-RESP-13) applies with perception at `co_location` or `full`; with perception `none`, a character viewpoint receives every beat except `think` and `latent`]* No player belief partition is introduced. `omniscient` is an author/GM-only mode, never a player default *[superseded by ADR-091: returned only when named explicitly, never a default; who may name it is the consumer's decision]*. Thought privacy is restated as **non-disclosure**: `think` content is never returned verbatim, paraphrased, or as narrated inner state, while inference from a character's own verified, perceived `say`/`do` is legitimate.
**Consequences:** The player-facing guarantee is now testable (property OUTPUT-VIEWPOINT, contract C-12, S-WHISPER player variants). Idempotent results are tied to the viewpoint of the original request.

---

### ADR-048 — Support requires coverage; instants are first-class
**Status:** Accepted. Refines ADR-035.
**Amended by:** ADR-058 — instants: exact equality, coarse compatibility, and event identity are distinct; "match at the coarser granularity" is superseded.
**Context:** "Same proposition, same polarity, overlapping time → supported" let canon about 2010–2014 support a claim about 2000–2020. Punctual events had no valid representation: `[t, t)` is empty.
**Decision:** `supported` requires the claim's valid-time to be **fully covered** by the union of matching holder intervals, under every admissible reading of approximate bounds. Partial coverage yields `uncertain(partial_coverage)` with covered and uncovered sub-intervals; the remainder is re-checked deterministically as its own claim (normally novel → permission check; if not permitted, repair narrows the claim). Contradiction still needs only overlap. Valid-time is an interval `[start, end)` with `start < end` or an **instant** with a granularity; an instant is covered when `start ≤ t < end`, and instants match when equal at the coarser granularity *[superseded by ADR-058: support needs the same or a finer granularity; a coarser record leaves the claim `uncertain(granularity_refinement)`]*.
**Consequences:** No inflated support; the model is not consulted for partial coverage. Property SUPPORT-COVERAGE; U-15 adds the counterexample.

---

### ADR-049 — Functional uniqueness is over a declared key; distinct facts stay distinct; exhaustiveness only by declared closed sets
**Status:** Accepted. Refines ADR-035.
**Context:** The core vocabulary treated "undergraduate institution" as functional per person, and S-TENNIS normalized "played on Florida's college team as an undergrad" into it. Playing for a team does not establish enrollment or which institution awarded a degree, and people can hold several degrees or a jointly awarded one. The tests would have enforced the very false contradictions the verifier exists to prevent.
**Decision:** Cardinality is declared over a **uniqueness key** (subject plus named key qualifiers); non-key qualifiers never participate; objects may be sets where the domain allows (joint awards). Education uses distinct core predicates — `enrolled_at`, `attended_program`, `member_of` (role), `degree_awarded` keyed by (person, degree ID) — with **no core implications** between them; authors may declare such rules where they hold. Exhaustiveness ("his only undergraduate degree", "enrolled only at OSU in those years") is expressed by explicit **closed sets**, never inferred; adding to a closed set over overlapping time is a contradiction.
**Consequences:** The Florida/OSU case is contradicted only when the story actually says enough to make it so, and the reason is cited. Scenarios share a documented Bobby education fixture.

---

### ADR-050 — Weak predicates forbid inferred exclusivity, not direct negation
**Status:** Accepted. Refines ADR-035.
**Context:** FR-PROP-4 and CONTRADICTION-SOUNDNESS said an auto-registered predicate could never yield `contradicted`, while FR-PROP-8/10 required explicit opposite polarities to contradict. "Bobby apprenticed with this blacksmith" vs "Bobby did not apprentice with this blacksmith" fell between them.
**Decision:** Weak semantics prevent contradictions from cardinality, exclusion, implication, containment, or closed sets the predicate was not given. A **direct polarity conflict** on the identical normalized proposition over overlapping time is `contradicted` for every predicate, weak ones included. Other semantic questions on weak predicates still go to the model residual.
**Consequences:** One consistent rule; the property test asserts both directions (weak predicates never yield inferred contradictions, and always yield direct-polarity ones).

---

### ADR-051 — External effects follow a staged, durable lifecycle with a recorded in-flight point
**Status:** Accepted. Refines ADR-038.
**Amended by:** ADR-054 — staged turns are the explicit exception to atomic turns; ADR-055 — only unconditional effects and reservations commit at intent; ADR-062 — configuration changes never touch committed stages.
**Context:** The outbox committed the turn before dispatch, synchronous confirmation waited before final composition, and composition is verified — but nothing said what remained committed if the action succeeded and composition failed, or the process crashed before the response was stored. Separately, "cancel until handed to the provider" depended on an unrecorded network moment.
**Decision:** A turn with an external effect commits in stages — intent (turn + outbox row), claim, outcome, outcome step, stored response — each durable and tracked in the operation's status (FR-EXT-16). Later failures never roll back earlier stages; retries and a recovery sweeper resume from the last committed stage, with deterministic outcome narration and deterministic recomposition as fallbacks (FR-EXT-17). The **claim commit**, made before any send, is the in-flight point: cancellable before it, `reconciliation_required` after it, sent or not (FR-EXT-12). Providers declare whether they deduplicate dispatch keys; non-deduplicating providers are never auto-re-sent, and ambiguous rows become `unknown` (FR-EXT-18). A synchronous wait that exceeds its budget degrades to asynchronous semantics.
**Consequences:** No state in which a confirmed action is believed undone, and no promise of cancellation Epistrel can't keep. Turns with synchronous effects return later than plain turns and occupy the story's in-flight slot until completion. Tests S-EFFECT-CRASH, OUTBOX-SAFETY.

---

### ADR-052 — Configuration is versioned per operation; interpretive changes revalidate, prospective changes don't
**Status:** Accepted. Refines ADR-029 and ADR-040.
**Amended by:** ADR-057 — the rebase cascades through same-turn dependencies; ADR-062/072 — rebasing stops at committed stages; settled outcome effects are durable; ADR-084 — profile changes are configuration changes, made in two stages with an impact report
**Context:** Read sets covered propositions but not the configuration that interprets them. An author could change predicate cardinality, State permissions, perception rules, or player authority mid-generation; an operator could change provider ownership or policies.
**Decision:** Every configuration change gets a configuration revision; every operation records the revisions it ran under, and replay uses them (FR-CONC-14). Each setting is tagged **interpretive** (changes how existing work is verified, authorized, routed, or committed) or **prospective** (affects only new work); untagged means interpretive (FR-CONC-15). An interpretive change the operation depends on forces a rebase under the new configuration, then regeneration or conflict; prospective changes never disturb in-flight work. The same rule covers rewrite previews, background workers, and the dispatch claim (FR-CONC-16).
**Consequences:** Configuration races are handled by the existing rebase/regenerate machinery rather than a separate lock. Requires disciplined tagging in the config schema, enforced by a build-time test (U-18).

---

### ADR-053 — Thunk resolution is checked for retroactive obligations; author authority picks the outcome, not a bypass
**Status:** Accepted. Refines ADR-022 (thunk resolution, deferral bias, and author override) and ADR-021.
**Context:** FR-COMMIT-11 only softly discouraged binding an old thunk to an existing modeled character, and FR-COMMIT-12 said an author resolution needed no history rewrite because the fact was undetermined. But binding an old anonymous knock to Bobby creates past facts (Bobby was at the door at t) and knowledge (Bobby has known since t) that can conflict with his recorded position or later behavior.
**Decision:** Every resolution derives its **retroactive obligations** and runs the dependency search and epistemic checks against recorded history before committing (FR-COMMIT-14). Model-driven resolutions that conflict are rejected and steered elsewhere; the time-gap soft flag applies only to non-conflicting bindings. Author-directed resolutions keep their chosen outcome, but conflicts route through the normal amendment process with blast radius, resolution mode, and preview. Non-conflicting resolutions commit together with the bound character's backdated knowledge (provenance `resolution`).
**Consequences:** "Undetermined" no longer implies "consequence-free"; author authority is preserved without opening a hole in epistemic consistency. Tests U-19, S-THUNK-RESOLVE, S-DIRECT-GEORGE.

---

### ADR-054 — Ordinary turns are atomic; synchronous-effect turns are workflows of atomic stages
**Status:** Accepted. Refines ADR-002 and ADR-051.
**Amended by:** ADR-062 — configuration rebasing applies only to uncommitted stages; ADR-072 — inter-turn operations arriving during a staged turn wait for its completion.
**Context:** FR-EXT-16/17 let stages survive later failures, while FR-STORE-5, NFR-REL-1, and FR-MULTI-11 promised whole-turn atomicity. Both cannot hold for synchronous external effects.
**Decision:** The exception is explicit: ordinary turns commit atomically; a **staged turn** (one with a synchronous external effect) is a durable workflow whose stages are each atomic. Truth-changing stages each increment the timeline revision once — intent, outcome, completion — while claim and response storage do not. The story's in-flight slot is held from intent to completion. While incomplete, history reads show the turn as `incomplete` with only committed beats, and a fork copies committed stages and closes the turn in the fork as "outcome not known in this story" (FR-EXT-19).
**Consequences:** One coherent atomicity contract; crash tests assert stage boundaries for staged turns and whole-turn atomicity otherwise (I-01, X-03).

---

### ADR-055 — Internal effects contingent on external ones are unconditional, reserved, or conditional
**Status:** Accepted. Refines ADR-039 and ADR-051.
**Amended by:** ADR-061 — late settlements are positioned where they arrive, not at the requesting turn; ADR-066 — expired reservations never convert automatically.
**Context:** Committing all State effects at intent let a failed external payment leave the player with an unpaid sword, and gave no rule for partial success across several external actions.
**Decision:** Each State effect in a turn with external effects is **unconditional** (at intent), a **reservation** (held at intent, converted on confirmation, released on failure), or **conditional** on named external effects (committed only when all confirm). Transfers default: incoming internal side conditional, outgoing internal side reserved. Failure drops conditional effects and releases reservations; timeout/unknown keeps them pending (optional per-State expiry → release + reconciliation); partial success settles each effect by its own prerequisites, with no automatic compensation of external successes. Applies equally to asynchronous and consumer-applied confirmations (FR-STATE-14).
**Consequences:** Internal state never runs ahead of external reality; narration can't assert a conditional effect before it commits. Test S-CONDITIONAL-EFFECTS.

---

### ADR-056 — Confirmations create post-effect value versions; deltas are never applied to stale reads
**Status:** Accepted. Refines ADR-020 and FR-EXT-2 per-turn pinning.
**Context:** A value pinned at 300 for the whole turn conflicts with an outcome step after a confirmed 50-gold purchase ("250 left").
**Decision:** The original read stays immutable. A confirmation that returns authoritative resulting values records a new pinned version; otherwise one recorded post-effect read may be made; otherwise the exact result is `unknown` for narration. Each beat or step verifies against the latest version at or before its position. The engine never computes "300 − 50" itself, because other writers may have changed the value (FR-EXT-20).
**Consequences:** Outcome narration is verifiable without weakening pinning or replay. Providers declare `returns_resulting_values`.

---

### ADR-057 — Configuration rebase cascades through same-turn dependencies
**Status:** Accepted. Refines ADR-052.
**Context:** Re-running verification and routing after an interpretive change could leave Jenni's staged belief, and her reply built on it, intact after a perception change made beat 1 envelope-only for her.
**Decision:** The rebase recomputes exposures, removes staged acquisitions that no longer hold, and regenerates from the first beat whose antecedents or perceived inputs changed, together with every later beat depending on it; earlier beats are kept (FR-CONC-16). Beats record their render-input set so the cascade is a lookup.
**Consequences:** Same-turn causal chains can't survive the removal of their premise. S-CONFIG-RACE case (g).

---

### ADR-058 — Exact equality, coarse compatibility, and event identity are separate temporal relations
**Status:** Accepted. Refines ADR-048.
**Context:** Matching instants at the coarser granularity made "May 10" equal to both "May 10 09:00" and "May 10 17:00", manufacturing certainty and risking both false support and false event merges.
**Decision:** Instants carry granularity. Support needs a record at the same or finer granularity; a coarser record leaves the added precision `uncertain(granularity_refinement)`, handled like partial coverage. Coarse compatibility shows two times could agree, nothing more. Event identity comes only from an event ID or uniqueness key; only instants on the same unique event can contradict, and only when incompatible or different at a shared granularity.
**Consequences:** No precision is invented and no distinct events are collapsed. U-15 mixed-granularity cases.

---

### ADR-059 — Composition checks are viewpoint-specific; stored results are re-authorized on every read
**Status:** Accepted. Refines ADR-047.
**Amended by:** ADR-091 — stored results are bound to the viewpoint they were produced for; there are no roles to re-check.
**Context:** FR-RESP-8 forbade any `think` in composition while FR-RESP-12 allowed labeled thoughts in omniscient output, and a stored omniscient result could be replayed after its caller lost the author role.
**Decision:** Character viewpoints enforce thought non-disclosure; authorized omniscient output permits attributed, labeled thoughts, with traceability and attribution checks unchanged. Every return of a stored result — idempotent replay, cache, history read — re-checks the caller's current role and viewpoint permission. *[superseded by ADR-091: a stored result is returned only for the viewpoint it was produced for; there are no roles to re-check]*
**Consequences:** Consistent rules for both modes; authorization can't be outlived by a cached response. C-12 and OUTPUT-VIEWPOINT cover both.

---

### ADR-060 — A built-in world clock: deterministic pacing, bounded skips, scheduled-event checks
**Status:** Accepted. Refines ADR-030 (States) and ADR-027 (gives the valid-time coordinate its source).
**Amended by:** ADR-063, ADR-064, ADR-067, ADR-068, ADR-073 — see the inline markers in the Decision below.
**Context:** Story-time was a coordinate with no source, and roleplay models rush: a meeting six hours away arrives three messages later. Nothing in a transcript records how much time has passed, so models infer momentum instead.
**Decision:** Every story ships with a **world clock** — a built-in, story-owned State at minute resolution, on by default, and the source of story-time for every beat (FR-CLOCK-1,2) *[anchoring superseded in part by ADR-068: an exact offset plus a ranged absolute anchor; precision is never invented]*. In `paced` mode a deterministic **pacing rule** advances it per beat by `say`+`do` word count (<5 → 0, 5–50 → 1 min, >50 → 2 min; per-story table; sequential sums, simultaneous takes the max) with no model call (FR-CLOCK-6). *[Timing superseded in part by ADR-067: beats and steps have start/end times; advances attach to step boundaries; simultaneous turns end at the latest beat end.]* Narrated skips are declared as time-advance effects or, if undeclared, estimated by a new **time estimation** model purpose and recorded with their reason (FR-CLOCK-7). Players, authors, and the consumer may skip freely, including skips the player sets in motion without naming a time ("I fall asleep" → the model carries them to morning); **model-initiated skips** must narrate an established activity and stay under `max_unprompted_skip`, configurable per scenario and overridable per story (default 24 h; 0 forbids them) (FR-CLOCK-8). Appointments are `scheduled` propositions; advances stop at due events by default (FR-CLOCK-9) *[superseded in part by ADR-064 and ADR-073: canon-only events with a lifecycle, one factual stop each, the beat regenerated at the cutoff; schedule statements classified create / change / report; `possibly_due` pauses]*; time, time-of-day, elapsed-time, and appointment-status claims verify deterministically with tolerances (FR-CLOCK-10) *[superseded by ADR-063: narration checks against the clock; character claims check against the character's time belief and are classified by intent]*; and the clock and upcoming commitments are put in every generation context (FR-CLOCK-11) *[superseded by ADR-063: only orchestration sees the objective clock; a character's context gets that character's time belief and own schedules]*. Modes `manual` and `external` cover games that advance or own time themselves. One clock per story in this version.
**Consequences:** Pacing becomes a property of the engine rather than of the model's restraint; the 11:20 "I have to go to my five o'clock" is a contradiction, not a style issue. Costs: a word count per beat, a range query per skip, and an occasional estimation call. Tests U-20, U-21, CLOCK-MONOTONIC, S-DINER, S-MOVIE, S-CLOCK-TIMELINE, E-09.

---

### ADR-061 — Late settlements take effect where they arrive, re-verified, never backdated
**Status:** Accepted. Refines ADR-055.
**Amended by:** ADR-066 — one settled terminal outcome per effect; ADR-072 — "at the current head" is superseded by the next inter-turn slot `N⁺k`, and automatic settlement requires an active origin.
**Context:** FR-STATE-14 positioned an asynchronous settlement at the requesting turn. If turn 10 bought a sword, turn 20 said none were left, and confirmation arrived at turn 30, the corrected view through turn 20 would silently include the sword.
**Decision:** A late outcome is split into the requesting link, a timeline-independent outcome record, a settlement operation at the current head *[superseded by ADR-072: the next inter-turn slot `N⁺k`, and only for an active origin]*, an effective valid-time equal to the clock there, and knowledge arrival by perception at that position. Preconditions are re-verified at settlement; failures become `settlement_conflict` for reconciliation. Rewinds, swaps, and forks that exclude the settlement drop the in-story effect but keep the outcome, listed as `confirmed_unsettled`. Backdating requires an author amendment with blast radius (FR-EXT-21).
**Consequences:** History before arrival is never rewritten by a confirmation; reservations (when both sides are tracked) prevent most conflicts in the first place. Test S-LATE-SETTLEMENT.

---

### ADR-062 — Configuration rebasing stops at committed stages
**Status:** Accepted. Refines ADR-052 and ADR-054.
**Amended by:** ADR-072 — "after outcome" is split: before the outcome transaction commits its effects are validated by it; after it, settled effects are durable and change only by a separate authorized operation.
**Context:** Configuration rebasing could regenerate beats, while staged turns make earlier stages durable; a payment could succeed and then the inventory schema change before completion.
**Decision:** Rebasing touches only uncommitted work. Before intent: normal rebase. After intent, before claim: the attempt stands and the effect is revalidated at claim (cancelled if no longer allowed). After claim or outcome: the outcome is preserved; conditional effects, reservation settlements, and the outcome step are revalidated *[superseded by ADR-072: the outcome transaction validates these when it commits; afterwards its settled effects are durable, and only pending work is revalidated (FR-CONC-16)]*; anything the new configuration forbids is reported as `settlement_conflict`, and the outcome is narrated truthfully (FR-CONC-16).
**Consequences:** No external action is ever treated as an ordinary regenerable turn. S-CONFIG-RACE (h)–(j).

---

### ADR-063 — Time claims use the three speech dimensions; characters get only the time they can know
**Status:** Accepted. Refines ADR-060 and ADR-036.
**Context:** FR-CLOCK-10 contradicted every wrong time claim, ignoring sincere errors, lies, and jokes; FR-CLOCK-11 gave every character the exact objective clock with no knowledge path.
**Decision:** Narration checks against the clock; character speech, thought, and action check against the character's **time belief** and are classified by intent. Time belief comes from a per-story `time_awareness` (`exact` default, `approximate`, `none`) overridden by acquired beliefs (a stopped watch, testimony, a gap in perception). The anti-rush check is the belief case: acting on an appointment before it is due *by one's own belief*, without deceptive intent, is repaired; pretexts are lies. Character contexts receive their time belief and their own schedules; only orchestration and verification see the objective clock (FR-CLOCK-10,11,15).
**Consequences:** Broken watches, stalling lies, and fantasy-era vagueness all work, without weakening the anti-rush check. Tests U-21, S-CLOCK-BELIEF.

---

### ADR-064 — Scheduled events have a lifecycle; an interrupted skip is regenerated at the cutoff; beliefs don't stop the clock
**Status:** Accepted. Refines ADR-060.
**Amended by:** ADR-073 — schedule statements are classified as create / change (owner only) / report; a sincere statement about an existing event never writes canon; `possibly_due` is a refinement pause.
**Context:** A due-event stop clamped the clock but left the narration, actions, effects, and exposures beyond the cutoff unspecified, and nothing prevented the same appointment from blocking advancement repeatedly or a mistaken belief from stopping the world.
**Decision:** A character's sincere statement of their own plans is canon (they are its authority) *[superseded by ADR-073: a new owned plan writes canon and its owner's explicit change reschedules it; any other statement about an existing event is a report (FR-CLOCK-19)]*; other schedule beliefs are belief-only and never stop the clock. Canon events move `pending → due → surfaced → completed | missed | cancelled` (with `rescheduled` back to pending; `missed` after a grace period) and stop the clock at most once. A stop is applied before commit and the beat is regenerated to end at the interruption; nothing the original placed after the cutoff is committed; a player-requested skip is resolved as an interrupted attempt (FR-CLOCK-9,16).
**Consequences:** Interruptions read naturally and leave no orphaned post-cutoff state. Tests S-DINER, S-MOVIE, CLOCK-MONOTONIC.

---

### ADR-065 — Claim detection is layered, always on by default, and measured separately from verification
**Status:** Accepted. Refines ADR-017 (extraction backstop) and ADR-032.
**Amended by:** ADR-077 — the guarantee boundary is published; privacy is structural first, output scanning secondary; ADR-080 — a clean leak eval is evidence with an upper bound, not a guarantee
**Context:** The extraction backstop ran only when the structural parse looked incomplete, but valid channels don't prove every claim in their prose was declared: an undeclared possession change or a leaked secret can sit inside well-formed output. Verification accuracy on extracted propositions does not measure this failure.
**Decision:** Claim detection has three layers — declared structure, always-on deterministic detectors (including protected-content matching against facts hidden from the speaker), and model extraction on every render by default (`extraction_mode = always`; `gated` allowed with shadow sampling of what gating misses). It runs on renders, reactive narration, and the final composed message. The guarantee is stated precisely: every detected claim is checked; complete detection is a measured rate, NFR-QUAL-10 (≥ 95% recall on an adversarial set), reported per layer and mode (FR-VERIFY-13,14).
**Consequences:** One model call per render is added by default, consistent with the launch-conservative, reduce-by-evidence policy of ADR-017; the shadow data is what justifies switching a story to `gated`. Tests U-22, S-EXTRACT-ADVERSARIAL, E-10.

---

### ADR-066 — One settled terminal outcome per effect; expired reservations never convert automatically
**Status:** Accepted. Refines ADR-055 and ADR-061.
**Context:** An expired reservation is released and the resource may be reused; a late payout confirmation would then "convert" a hold that no longer exists. Duplicate and contradictory outcome reports were also unspecified.
**Decision:** Each outbox row has exactly one settled terminal outcome — the first recorded; duplicates are recorded and ignored; a conflicting later terminal outcome is listed `outcome_conflict` and never reverses anything. A success arriving after reservation expiry is re-verified against the current resource and listed `late_success_after_expiry`; nothing changes in the story until the author or consumer chooses **settle now** (re-verified at that moment) or **compensate** (FR-EXT-22).
**Consequences:** No automatic transfer of something the story has since given away. Test S-RESERVATION-EXPIRY.

---

### ADR-067 — Beats and steps have start and end times
**Status:** Accepted. Refines ADR-060.
**Amended by:** ADR-074 — the pacing increment goes through the due-event checks and is clamped, not regenerated; ADR-076 — simultaneous turns cut off after reconciliation; as-originally-recorded views at slot boundaries
**Context:** A single post-verification beat stamp could not place "she starts driving at 15:10; twenty minutes later she arrives."
**Decision:** Sequential beats start where the previous ended; simultaneous beats all start at the turn start, and the turn ends at the latest beat end. Advances attach to step boundaries and shift later steps; the pacing increment applies at a beat's end only when the beat has no in-beat advance. Each claim, exposure, and acquisition takes its step's start time; durative propositions span to the next step (FR-CLOCK-17).
**Consequences:** In-beat chronology is explicit and testable (U-23).

---

### ADR-068 — Partial anchors: an exact offset plus a ranged absolute anchor; never invent precision
**Status:** Accepted. Refines ADR-060.
**Amended by:** ADR-073 — `possibly_due` is a pause for anchor refinement, not an interruption, and not the event's factual stop; ADR-079 — retcon comparisons use clock offsets and the anchor's admissible range
**Context:** "Saturday morning" supplies neither a date nor a minute, yet the clock has minute resolution; anchoring could silently pick values.
**Decision:** The clock is an exact offset; its absolute time is an anchor of per-component values or ranges, positioned where established and shifted by offsets to other positions. Refinements narrow it; incompatible statements contradict. Checks undecided across the range return `uncertain`; scheduled events can be `possibly_due`, which stops advances conservatively *[revised per ADR-073: `possibly_due` pauses an advance at most once per anchor state and is not the event's stop]* and prompts the story to establish the time (FR-CLOCK-3,18).
**Consequences:** No fabricated dates or minutes; the anti-rush stop still works on loosely anchored stories. Test U-24.

---

### ADR-069 — Nested beliefs are explicit records, verified only against the holder's own view
**Status:** Accepted. Refines ADR-025.
**Context:** FR-EPI-2 required second-order beliefs, but the only representation given ("believes X was claimed") is a belief about a speech act, not "Jenni believes Bobby believes P."
**Decision:** A nested belief is a belief record whose content is another belief, with holder, attributed holder, inner proposition, attributed and own confidence, typed provenance, and three times; default depth 2. Claims about another's beliefs verify against the speaker's nested beliefs; the attribution's objective relation (what the other actually believes) is computed for labels only and never enters the speaker's context. Beliefs about speech acts (`said`) remain distinct (FR-BELIEF-15).
**Consequences:** Theory-of-mind scenarios are representable without leaking the attributed character's private state. Test S-NESTED-BELIEF.

---

### ADR-070 — Retrieval eligibility is separate from timeline membership; retirement is mandatory and budgeted
**Status:** Accepted. Refines ADR-033 (consolidation scope and retention defaults).
**Amended by:** ADR-072 — only pinned memories are exempt (admission-limited); commitments, thunks, and schedules need no hot-set protection; enforcement is eventual within a bounded lag; ADR-075 — overflow episodes have fixed deadlines
**Context:** NFR-SCALE-2 promised a bounded active working set, but consolidation adds records, archival was only a Should, and vectors were kept for all active records — so a long active timeline could grow its active records and vectors without bound.
**Decision:** Memories carry a retrieval tier (`hot` / `retired`) separate from membership. Mandatory retirement enforces per-character and per-story hot budgets (defaults 2,000 records per character, 50,000 vectors per story), retiring only consolidation-covered or below-floor records and never protected classes *[superseded by ADR-072 and ADR-075: only pinned memories are exempt; at the episode deadline uncovered records are retired anyway (FR-MEM-8)]*. Retired records stay true and verifiable, drop vectors by default, and rehydrate on demand. NFR-SCALE-2 now promises bounded hot sets and retrieved context, not bounded active records (FR-MEM-8).
**Consequences:** The bound is real and measurable; the honest limit (linear active history) is stated. Tests I-16, X-01.

---

### ADR-071 — Viewpoint-filtered working memory; consolidation preserves epistemic type
**Status:** Accepted. Refines ADR-026, ADR-033, and ADR-047.
**Context:** Calling scene context non-authoritative kept it out of canon but not out of character prompts, so a consumer transcript containing a whisper could still steer a character who never heard it. Separately, consolidation preserved visibility but not modality: public testimony could become objective fact in a reflection simply because its source was public.
**Decision:** A character's working memory is rebuilt from history through the same perception routing as memory writes and output, and consumer-supplied recent messages never enter a character's generation context (FR-MEM-11, FR-RESP-4). Consolidation keeps each input's epistemic type — "Bobby said P" stays a claim, beliefs stay beliefs — and states objective facts only from objective canon (FR-MEM-9).
**Consequences:** Unseen content can influence neither canon nor behavior; reflections cannot launder testimony into truth. Tests PRIV, S-WHISPER, S-CONSOLIDATION-SCOPE. This batch also corrected cross-document test statements (DETERMINED-SINGLE-VALUE per uniqueness key; TIME and S-REWIND view equality per FR-STORE-26; a deterministic violation fixture for E-08) and the whitepaper's repair example.

---

### ADR-072 — Inter-turn slots; late settlement needs an active origin; durable outcome transactions; budgets that hold for every story
**Status:** Accepted. Refines ADR-061, ADR-062, and ADR-070.
**Amended by:** ADR-074 — settlement valid-time is the clock immediately before the slot commits; ADR-075 — a story-wide pin cap and per-episode retirement deadlines.
**Context:** (1) A late confirmation could settle into a continuation that had discarded its purchase. (2) "The current head" had no ordering inside a position, so the checkpoint after a reply and after a later settlement were indistinguishable, and arrival during generation was undefined. (3) S-CONFIG-RACE(j) expected a configuration change to block a grant that the outcome transaction had already committed. (4) Hot budgets could be broken by never-retired classes, and retirement is asynchronous.
**Decision:** (1) Automatic settlement requires the requesting operation to be active; otherwise the outcome is recorded and listed `confirmed_discarded_origin` for explicit settle-now or compensate (FR-EXT-21). (2) Inter-turn operations (State sets, settlements) occupy slots `N⁺k` after turn N's completion; checkpoint `N` excludes them and is the default boundary, `N⁺k` includes them; slots follow N's variant; a settlement arriving mid-generation lands before the in-flight turn, which rebases; one arriving during an incomplete staged turn waits for it; settlements take the last completed turn's end time and don't advance the clock *[superseded by ADR-074: the clock immediately before the slot commits, including earlier slots' advances]* (FR-STORE-31, FR-STATE-9). (3) Configuration changes before the outcome transaction are validated by it; after it, settled effects are durable and change only through separate authorized operations (FR-CONC-16). (4) Only pinned memories are exempt from the hot budget, and pins are admission-limited; commitments, thunks, and schedules are structured records needing no hot-set protection; enforcement is eventual within `retirement_max_lag`, falling back to retiring uncovered records, with overflow reported (FR-MEM-8, NFR-SCALE-2).
**Consequences:** No storyline receives consequences of an action it doesn't contain; positions between turns are addressable and reconstructible; committed stages stay committed; the hot-set bound is achievable for every permitted story. Tests S-LATE-SETTLEMENT, S-CONFIG-RACE (j1/j2), I-16, X-01.

---

### ADR-073 — Creating a plan is not reporting a schedule; `possibly_due` is a refinement pause, not an interruption
**Status:** Accepted. Refines ADR-064 and ADR-068.
**Amended by:** ADR-074 — `possibly_due` is defined over the anchor range `[lo, hi)`: entered when `hi` passes the event, `due` when `lo` reaches it.
**Context:** "My meeting is at five" could either create Jenni's plan or report an existing meeting's time; treating every sincere statement as canon would let a misremembering reschedule a real meeting. And `possibly_due` stopped the clock without saying whether that was the event's one stop, whether it implied the event was due, or how refinement resolved it.
**Decision:** Schedule statements are classified by entity resolution as **create** (a new owned plan → canon), **change** (explicit change language or a declared change by the event's **owner** → reschedule), or **report** (anything else about an existing event → an ordinary claim, never writing canon; sincere errors need a legitimate antecedent). Ownership defaults to the creator (FR-CLOCK-19). `possibly_due` is a distinct status that pauses an advance at most once per anchor state to prompt establishing the time; it never narrates the event as due and is not the event's factual stop. Refinement returns the event to `pending`, makes it `due` (its one factual stop), or surfaces it as overdue/`missed`; passing the latest admissible due time resolves it factually (FR-CLOCK-18).
**Consequences:** Canon schedules change only by their owners; loosely anchored stories still can't rush past meetings, and never invent a "the meeting is now" interruption. Tests S-DINER, S-ANCHOR-MEETING.

---

### ADR-074 — Settlements take the clock at their slot; pacing advances are checked; `possibly_due` is defined over the anchor range
**Status:** Accepted. Refines ADR-072, ADR-067, and ADR-073.
**Amended by:** ADR-076 — simultaneous turns: cutoff computed after reconciliation; narrated overruns regenerated.
**Context:** (1) Settling at "the last completed turn's end time" ignored clock advances made by earlier inter-turn slots (turn 14 ends 11:00, `14⁺1` sets 12:00, a `14⁺2` settlement would be stamped 11:00). (2) The end-of-beat pacing increment was not routed through the due-event check, so a two-minute beat from 15:59 could pass a 16:00 appointment. (3) "Possibly due from the earliest admissible moment" was ambiguous and the S-ANCHOR-MEETING expectations were inconsistent with it.
**Decision:** (1) A settlement's valid-time is the clock immediately before its slot commits, including earlier slots' advances (FR-EXT-21). (2) Every advance — including pacing increments and the simultaneous turn-end advance — goes through the scheduled-event and partial-anchor checks; a stop inside a pacing increment clamps the beat's (or turn's) end without regeneration and surfaces the event to what follows (FR-CLOCK-17). (3) With the current time as an admissible range `[lo, hi)`, an event at E is `possibly_due` while `lo < E < hi` — entered when `hi` passes E — and `due` once `lo ≥ E`, which is its factual stop; `missed` follows once `lo` passes E plus grace (FR-CLOCK-18).
**Consequences:** No stale settlement timestamps; no appointment can be crossed by ordinary dialogue; the anchor tests use explicit ranges. Tests S-LATE-SETTLEMENT, U-23, S-DINER, S-ANCHOR-MEETING.

---

### ADR-075 — Story-wide pin cap and per-episode retirement deadlines; positions include inter-turn slots everywhere
**Status:** Accepted. Refines ADR-072, ADR-025, and ADR-041.
**Context:** Pins were capped per character while the vector budget is per story, so the story-wide bound wasn't stated; "within budget after `retirement_max_lag`" didn't say what happens with repeated bursts; exposure frames and FR-STORE-31's wording still assumed turn-only positions after inter-turn slots were introduced.
**Decision:** Add `max_pinned_per_story` (default 2,000) and state both aggregate bounds explicitly. Overflow is handled per **episode**: it starts at the first over-budget commit, its deadline is fixed (`retirement_max_lag` turns) and does not reset while it lasts, and a new episode can start only after the set returns within budget. A full narrative position is `(turn, beat, step)` or an inter-turn slot `N⁺k`; exposure frames and lazy resolution use either; checkpoints and forks support both `N` and `N⁺k` boundaries (FR-MEM-8, NFR-SCALE-2, FR-BELIEF-5,6, FR-STORE-31).
**Consequences:** The bounds are explicit for any number of characters and any burst pattern; settlement-time exposures resolve lazily without backward pollution. Tests I-16, X-01, EXPOSURE-FRAME, S-LATE-SETTLEMENT (6).

---

### ADR-076 — Simultaneous turns cut off after reconciliation; as-originally-recorded views at slot boundaries
**Status:** Accepted. Refines ADR-074, ADR-067, and ADR-027.
**Context:** Simultaneous beats are timed independently and cannot see each other, so one beat could create an appointment while another narrates consequences past it; and the as-originally-recorded view was defined only at "the transaction-time when message N was recorded," which precedes any inter-turn slots.
**Decision:** A simultaneous turn's due-event check runs after reconciliation across all beats, including events created in the same turn; the earliest stop is the turn's cutoff; pacing-only overruns are clamped, narrated overruns are regenerated under FR-CLOCK-16; no committed step or effect lies after the cutoff, and every beat end is at or before the committed turn end (FR-CLOCK-17). The as-originally-recorded view at `N` uses turn N's completion commit and at `N⁺k` slot k's commit, excluding later slots, amendments, and derived records (FR-STORE-23).
**Consequences:** Clock cutoffs and audit views are both well defined at every boundary. Tests U-25, S-ORIGINAL-AT-SLOT; I-16 corrected to bound only outside overflow episodes.

---

### ADR-077 — "The model proposes; the engine validates": a stated guarantee boundary, with privacy structural first
**Status:** Accepted. Refines ADR-001, ADR-065, and ADR-044.
**Amended by:** ADR-084 — the guarantee table is published per engine profile preset; guarantees follow enabled capabilities; ADR-106 — creative quality (voice, plot adherence) is outside the guarantee; the inputs are guaranteed; ADR-080 — budgets and ordered degradation bound the model-dependent steps; ADR-087 — output claims are precise: only detected claims are checked, per profile
**Context:** The whitepaper said every rendered output is verified and that private content does not leak, while the requirements separate checking a detected claim from detecting every claim (95% recall target) and gate privacy on a finite labeled set. Several consequential steps — extraction, salience, time estimation, entailment, belief adjudication, repair — are model-dependent, so "the LLM is a renderer" overstated the model's confinement.
**Decision:** State the boundary as *the model proposes; the engine validates structured claims, effects, and permissions*, and publish which steps are deterministic and which are model-dependent with their metrics (FR-VERIFY-15). Make privacy **structural first**: a character's context holds only what that character is authorized to know; output scanning is a secondary, measured check, and zero observed leaks is reported as evidence with an upper bound (NFR-PRIV-4, NFR-QUAL-1).
**Consequences:** Claims in the documentation match what the system can deliver; the strongest privacy property no longer depends on detection recall. Test C-14.

---

### ADR-078 — Request roles are separate fields with separate authorization
**Status:** Accepted. Refines ADR-033 (authorization) and ADR-047.
**Amended by:** ADR-091 — who may act as which character is the consumer's decision; the fields stay distinct.
**Context:** "A player may respond and ingest for their own character" could reject ordinary play (the player asking NPCs to reply) or, read loosely, let a player act as an NPC.
**Decision:** Respond and ingest carry the requesting principal, actor character, render characters, and output viewpoint as distinct fields (FR-RESP-1). A player may act only as an assigned character, may trigger permitted NPC responses, gains no authorship of those NPCs beyond `co_author` narration, and never gains access to their private state (FR-AUTH-2,7). *[superseded by ADR-091: who may act as which character is the consumer's decision; render-trigger rules and `player_authority` remain story rules, FR-AUTH-3]*
**Consequences:** Ordinary play is explicitly allowed; impersonation is explicitly rejected *[superseded by ADR-091]*. Test C-13.

---

### ADR-079 — Retcon eligibility is per correction part, in clock offsets where possible, with a configurable rule for undecidable timing
**Status:** Accepted. Refines ADR-027, ADR-042, and ADR-068.
**Context:** Amendments are included at a checkpoint by comparing their valid-time to the checkpoint's story-time, but with a partially anchored clock that comparison may have no single answer, and it was unclear whether eligibility applied per amendment or per part.
**Decision:** Evaluate eligibility per correction part: record corrections follow their record; world-proposition corrections compare valid-time to checkpoint time in clock offsets whenever both can be expressed that way (exact regardless of anchoring), and otherwise over the anchor range — certainly before → in, certainly after → out, undecidable → per-story `ambiguous_retcon_timing`: `include_if_possible` (default; included and flagged `timing_uncertain`) or `ask_author` (a required rewrite question in any resolution mode, with the answer stored as an explicit position bound) (FR-STORE-20).
**Consequences:** Backstory retcons survive in loosely anchored stories by default; stories that need precision can require it. Test S-RETCON-TIMING.

---

### ADR-080 — Per-turn model budgets and deadlines with ordered degradation; honest claims about leaks and erasure
**Status:** Accepted. Refines ADR-017, ADR-065, ADR-077, and ADR-046.
**Amended by:** ADR-122 — opt-in compaction is a second exception to immutability; ADR-123 — the default budget and the safety reservation formula; ADR-133 — guardrail calls count once per text checked, and staged turns reserve their outcome step; ADR-087 — Lite's call boundary is one generation plus model-assisted guardrail calls; ADR-144 — audit-record retention expiry is a third exception to immutability
**Context:** Extraction on every render, multi-character turns, retries, repair, time estimation, and composition checks can add several model calls per character, with no per-turn limit, deadline, or defined degradation; engine-only latency targets said nothing about full-turn time. Separately, a clean zero-leak eval must not become a product claim, and erasure is a real exception to "the event log is the truth."
**Decision:** Every turn runs under a model-call budget (default 3 + 4 per primary character *[superseded by ADR-123 and ADR-133: 4 + 5 per primary + G + O, where G counts each model-assisted policy once per text it checks and O reserves a staged turn's outcome step, with a safety reservation formula]*) and a deadline (default 45 s, excluding external waits), with claim-detection calls reserved. Exhaustion degrades in a fixed order — polish, extra repairs, entailment, time estimation, extra primaries, then beat replacement by the safe fallback — and never releases unchecked content; the response reports what was degraded (NFR-PERF-6,7). End-to-end latency is measured and published per model tier (NFR-PERF-8). Clean leak evals are stated as "none observed in n, upper bound ≈ 3/n," never "leak-free" (NFR-QUAL-1). Erasure is the documented exception to event immutability *[revised per ADR-122: opt-in compaction is a second, explicit exception, with the same replay basis]* *[revised per ADR-144: audit-record retention expiry is a third exception; no story truth changes]*: replay, forks, determinism, and restore are defined over the post-erasure log, and audit/backup tooling treats erasure markers as expected gaps (FR-STORE-1, NFR-DETERM-1, FR-ADMIN-7).
**Consequences:** Consumers get bounded cost and time with explicit, safe degradation; documentation claims match the evidence; recovery behavior after erasure is unsurprising. Tests P-08, P-09, S-ERASURE-BOUNDARY, C-14.

---

### ADR-081 — A versioned character catalog, used by snapshot; every character gets a companion scenario
**Status:** Accepted. Builds on ADR-019 (self-contained stories) and ADR-023 (modeling; player has no interiority).
**Amended by:** ADR-088 — characters define personality only; personas are not catalog characters; the companion scenario holds the character's default world; ADR-090 — companion versions are separate, pinned to a character version, and published atomically; ADR-094 — characters carry a content rating from the deployment scale; ADR-095 — characters carry a true age; ADR-097 — images are optional and characters carry a visual identity; ADR-098 — Epistrel can generate images.
**Context:** Stories need reusable starting points for characters — appearance, personality, interaction style, starting knowledge, and an image with a reproducible generation seed — and a simple "talk to this character" entry point.
**Decision:** A catalog of characters, outside story data, with draft/publish/archive and immutable numbered versions (FR-CAT-1..3). A primary image with generation metadata is required to publish; Epistrel stores media but never generates it (FR-CAT-4). *[superseded by ADR-097 and ADR-098: images are optional, and Epistrel can generate them]* Each character automatically gets a **companion scenario** with one NPC slot locked to it plus a player slot (FR-CAT-5). Catalog characters may be player personas, contributing only public facts (FR-CAT-6) *[superseded by ADR-088: players play lightweight personas, never catalog characters]*. Ad-hoc story characters stay out of the catalog unless promoted, without their memories by default (FR-CAT-7,8). Stories are created from **snapshots** of exact versions; later catalog changes never reach them (FR-CAT-9, FR-SCN-9).
**Consequences:** The engine and Orchestrator are unchanged; the catalog is a library of starting points. Tests C-15, I-17, S-COMPANION, S-PROMOTE.

---

### ADR-082 — Scenarios with role slots, a configurable starting-cast limit, deterministic seed merging, and an atomic genesis
**Status:** Accepted. Builds on ADR-030 (States), ADR-060 (clock), ADR-035 (propositions), and ADR-072 (positions).
**Amended by:** ADR-087 — seed precedence is applied by seed type; ADR-088 — merge rule is facts: scenario > character, personality: character > scenario; roles define situation, not personality; ADR-094 — rating mismatches between members and the scenario are warnings, judged by guardrail sets; ADR-141 — scenario fields have classes; only knowledge seeds become facts; genesis materializes by capability; ADR-091 — Epistrel serves `card` and `full` projections; the consumer decides which to show
**Context:** A story's starting point — world, cast, starting state, clock, plot drivers — should be reusable, and players should be able to choose who plays each part without breaking the setup.
**Decision:** Versioned scenarios carry presentation and media, world and lore, roles, seeded memories with holders and visibility, State trackers, the starting clock, scheduled events, predicates and rules, the opening, plot hooks, objectives, hidden director guidance, planned reveals, settings defaults, required providers, and overridable fields (FR-SCN-2). Roles are `npc` or `player`, `open` / `default` / `locked`, with eligibility filters, role seeds, relationships, and name overrides (FR-SCN-3). The starting cast, player slots included, is limited by `max_characters_at_start` — deployment default 16, overridable per scenario and per story creation, under an optional operator ceiling (FR-SCN-4). Personality always comes from the chosen character; objective conflicts resolve world > role > catalog backstory and are reported (FR-SCN-7). Creation is one atomic **genesis** operation at position 0 with seeds as `seed`-provenance author acts, and the opening narration as turn 1 (FR-SCN-8,9). Hidden fields are never returned to players (FR-SCN-10) *[superseded by ADR-091: Epistrel serves `card` and `full` projections, and the consumer decides which to show]*.
**Consequences:** A story can be rewound to its opening; scenarios can't be spoiled by browsing *[revised per ADR-091: when the consumer shows players the `card` projection]*; ensemble casts are bounded but adjustable. Tests U-26, U-27, I-17, S-FAMILY.

---

### ADR-083 — Media as immutable assets in a pluggable blob store
**Status:** Accepted.
**Amended by:** ADR-094 — media ratings use the deployment's content-rating scale; ADR-097 — large media are delivered by byte range, signed URL, and resumable upload; ADR-098 — Epistrel can generate images and checks them against image policies; ADR-113 — drafts and live candidates count as references, and the general sweep skips candidates.
**Context:** Characters and scenarios need images and videos; the deployment must stay OSS-only and self-hostable, and stories must stay independent of catalog deletions.
**Decision:** Immutable, content-addressed assets in a `BlobStore` port — local filesystem by default, any S3-compatible endpoint optionally — with metadata (including image generation metadata) in Postgres. Catalog versions and story snapshots reference assets by ID; a periodic sweep purges unreferenced assets *[refined by ADR-113: drafts and live candidates count as references]*, and erasure purges directly. Epistrel records declared content ratings and offers a consumer moderation hook but does not moderate or generate media (FR-MEDIA-1,2). *[superseded in part by ADR-098: Epistrel can generate images and checks images against image policies]*
**Consequences:** No media duplication per story, no reference counting in the hot path, and no commercial dependency. Tests C-15, I-17.

---

### ADR-084 — Engine profiles: capability modules with validated dependencies, followed live, changed by warn-then-confirm
**Status:** Accepted. Refines ADR-017, ADR-052, and ADR-077.
**Amended by:** ADR-087 — quality floors are claimed only for certified configuration fingerprints; ADR-091 — the only hard refusals are invalid configurations and calls without a valid service credential; ADR-134 — delivery milestones follow the capability map; ADR-138 — FR-PROF-6 is split; data-producing changes are FR-PROF-18; ADR-144 — the capability map covers every requirement area; perception levels are `none`, `co_location`, and `full`; the clock dial is off or on
**Context:** Cost and latency vary enormously with the capabilities in use, and not every story needs all of them; tiers of a product may offer different levels. Many settings interact, so a flat bag of dials would allow combinations that cannot work.
**Decision:** An **engine profile** is a named, versioned set of capability modules (pipeline, memory, character knowledge, perception, verification, commitment, clock, States, multi-character, repair, budgets, models) with dials, validated against a dependency graph; presets Lite, Standard, Full, and Max ship as editable rows (FR-PROF-1..4). Stories follow the latest profile version plus their own overrides (FR-PROF-5,6). Settings are classified **behavioral** (apply next turn, never need a backfill) or **data-producing** (an increase needs a backfill or `forward_only`; a decrease leaves data dormant) (FR-PROF-6,8). Every potentially degrading change is two-stage: an impact report first, then application with `ignore_warnings`; only invalid configurations and authorization failures are refused (FR-PROF-7) *[superseded by ADR-091: there is no user authorization in Epistrel; only invalid configurations and calls without a valid service credential are refused]* *[revised per ADR-144: and, until upgrade modes exist, a data-producing increase for a story with history is `upgrade_unavailable`; that story keeps its configuration (FR-PROF-7)]*. Tier entitlement is the consumer's concern. Guarantees are mapped to the modules that provide them and published per preset (FR-PROF-12,13). Guardrails are not a module: if policies are defined, no profile bypasses them (FR-PROF-14).
**Consequences:** Cost and quality become explicit, comparable choices per story; implementers get one place to see which requirement applies when. Tests U-28, C-16, C-17, S-PROFILE-SWITCH.

---

### ADR-085 — A Lite pipeline that matches today's roleplay engines, on the same catalog and transcript
**Status:** Accepted.
**Amended by:** ADR-087 — a Lite turn is one generation call; extra calls only for model-assisted guardrails.
**Context:** The bottom of the range should be as cheap and simple as current roleplay engines, without forking the product.
**Decision:** The `lite` pipeline assembles one prompt — custom instructions, scenario and character text, pins, a rolling auto-memory, recent messages — and makes exactly one generation call — plus a guardrail-check call only for model-assisted policies and bounded regeneration if a guardrail trips — storing the transcript like every other pipeline; Lite performs no claim detection, so no detection calls are reserved. The Orchestrator compresses older messages into position-tagged summary segments that follow timeline membership. Character separation in Lite is by instruction only, which the guarantee table states; the clock is available in a prompt-only form and States only via the API (FR-LITE-1..6).
**Consequences:** One API, catalog, and storage model across the whole range; Lite stories can be upgraded later. Test I-18.

---

### ADR-086 — Backfill re-derives from the stored transcript, pauses the story, and resolves contradictions by policy
**Status:** Accepted. Refines ADR-028 (operations) and ADR-079 (conflict handling by policy).
**Context:** Because the full transcript is always stored, a story can gain capabilities mid-life — but the old turns lack the structured data, and text produced without verification may contradict itself.
**Decision:** Every profile stores the full transcript (FR-PROF-11). A backfill re-derives structure from it — never regenerating or altering a message — in parallel scene-sized chunks followed by one ordered reconciliation pass, with provenance `backfill` at a lower trust tier and inferred perception marked; it is resumable and cancellable, and the story is paused while it runs (FR-PROF-9). Contradictions follow `backfill_conflict_policy`: `last_mention` (default), `first_mention`, or `ask` (story stays paused until resolved or resumed with `ignore_warnings`, leaving facts `uncertain`) (FR-PROF-10). Downgrades keep structured data dormant; later upgrades backfill only the turns since (FR-LITE-6).
**Consequences:** "Start cheap, upgrade later" is real; history the player saw is never changed. Tests I-19, S-PROFILE-SWITCH.

---

### ADR-087 — Precise output claims, Lite's call boundary, seed merging by type, and certified configurations
**Status:** Accepted. Refines ADR-077, ADR-080, ADR-082, ADR-084, and ADR-085.
**Amended by:** ADR-107 — `stale` applies only to a fingerprint that was certified and later invalidated; never-certified is `uncertified`; ADR-142 — seed-type conflicts are detected only with the structured pipeline; Lite drops nothing; ADR-088 — seed precedence follows facts from the scenario, personality from the character
**Context:** (1) The whitepaper still promised a "verified" message, contradicting the guarantee boundary and Lite's lack of structured checking. (2) Lite was described both as "one model call" and "one generation call, subject to guardrails," while budgets reserved claim-detection calls Lite never makes. (3) Seed precedence was defined for objective facts only, yet false beliefs legitimately coexist with canon and secrets can share holders. (4) Floors applied to presets, but profiles, overrides, and models can change.
**Decision:** (1) Responses are "finished messages whose detected claims were checked under the story's profile"; Lite does no structured claim checking (FR-RESP-1). (2) A Lite turn is exactly one generation call; extra calls arise only from model-assisted guardrail policies and bounded guardrail regenerations; guardrails are deterministic by default; Lite reserves no detection calls (FR-LITE-7, FR-GUARD-9). (3) World > role > catalog precedence applies by seed type and only to genuine same-type conflicts: objective contradictions; same-holder belief conflicts; secret visibility (holders unite); same-holder nested beliefs; same-identity scheduled events; functional relationships (FR-SCN-7) *[revised per ADR-142: these conflicts are detected only with the structured pipeline; under `lite` nothing is dropped and the check is reported as `check_unavailable`]*. (4) Floors are claimed only for **certified configuration fingerprints**; responses and impact reports state capabilities enabled and quality status (`certified` / `uncertified` / `stale`); quality-relevant changes produce new fingerprints and warnings; optional canaries detect drift (FR-PROF-15..17).
**Consequences:** Every document makes the same, defensible claims; Lite's cost is predictable; seeding preserves legitimate false beliefs and shared secrets; tiers can be sold on measured quality rather than enabled features. Tests U-27, S-FAMILY, I-18, C-18.

---

### ADR-088 — Facts from the scenario, personality from the character; personas are not catalog characters; only public cards are public
**Status:** Accepted. Refines ADR-081, ADR-082, and ADR-087.
**Amended by:** ADR-089 — persona details are seeded by audience; acquaintances hold the personality description as beliefs; ADR-091 — Epistrel serves `card` and `full` projections and the consumer chooses which to show; ADR-107 — personality warnings use the shared validation report shape, naming the ignored text and the rule; ADR-142 — fact-conflict detection needs the structured pipeline; Lite renders scenario facts first and drops nothing; ADR-090 — character and companion versions are separate, pinned, and published atomically; ADR-141 — scenario fields have classes, materialized by capability
**Context:** The catalog mixed two different things into characters — who someone is, and what is true in their world — which made characters carry secrets and backstory into every scenario, forced field-by-field privacy rules, and let scenarios and characters fight over personality. Catalog characters were also allowed as player personas, although the platform always plays catalog characters.
**Decision:** A catalog character is a **definition of who someone is** — appearance, personality, likes and dislikes, behavioral dispositions (threat response, dominance, honesty, and similar), values, interaction style — with a public card of name, public description, and images (FR-CAT-2). World truths about a character belong to scenarios; the companion scenario holds the character's default world and is never brought into other scenarios (FR-CAT-5). The merge rule is **facts: scenario > character; personality: character > scenario**; roles define situation, not personality, and same-type fact conflicts keep the by-type rules (FR-SCN-3,7) *[revised per ADR-142: fact conflicts are detected only with the structured pipeline; a Lite story renders scenario facts first and drops nothing]*. Scenario seeds carry holder, an extensible **audience** (`self`, roles, `acquaintances`, `perceivable`, `public`), and **disclosure** (`open`, `guarded`, `secret`) (FR-SCN-8). Players play lightweight **personas** (name, gender, age, brief personality) in a role the scenario fixes; personas are not catalog entries and their personality text is not NPC knowledge (FR-CAT-6) *[superseded by ADR-089: seeded by audience — perceivable traits to all, name/age/description to acquaintances]*. Only the public cards — scenario name, public description, media; role label and setup description; character name, public description, images — are shown to players, and public descriptions are never seeded (FR-SCN-2,10). *[superseded by ADR-091: Epistrel serves `card` and `full` projections, and the consumer decides which to show]*
**Consequences:** Characters are portable across scenarios without dragging their histories along; scenarios can't spoil themselves or override who a character is; privacy rules collapse to "public card vs everything else." Tests U-27, S-FAMILY, S-COMPANION, S-PROMOTE, C-15, C-19.

---

### ADR-089 — Persona details are seeded by audience, like any other knowledge
**Status:** Accepted. Refines ADR-088.
**Amended by:** ADR-107 — the persona description is never objective canon; only the name and true age are canonical.
**Context:** ADR-088 kept a persona's personality as player guidance only. But a persona is something the player asserts about themselves, and characters would plausibly know or infer much of it: strangers see a man in his twenties; family and coworkers know his name and that he has a temper.
**Decision:** At genesis a persona is seeded by audience (FR-CAT-6): gender, visible appearance, and an **apparent age band** are perceivable to anyone who meets the player (exact age is not); name, exact age, and the short description are beliefs of the characters the player role's relationships make acquaintances, revisable by what the player actually does; strangers learn more only through play. The description also guides the orchestration's narration of the player character.
**Consequences:** NPCs treat the player as someone with a known history where the scenario says they have one, and make ordinary inferences elsewhere. Tests S-FAMILY, C-19.

---

### ADR-090 — Character and companion versions are separate, pinned, and published atomically
**Status:** Accepted. Refines ADR-081 and ADR-088.
**Amended by:** ADR-094 — a rating difference between a character and its companion is a warning, not a publish failure.
**Context:** "Each published character version publishes a matching companion version" left open what happens when the companion changes alone, when both have drafts, when two editors collide, or when publishing half-fails.
**Decision:** Separate version sequences linked by a pin: each companion version records the character version it locks (FR-CAT-10). A companion-only publish pins the latest published character; a character-only publish creates the character version plus a re-pinned copy of the latest published companion in one transaction, leaving any companion draft unpublished but re-pinned; "publish together" publishes both drafts as a pair. Every publish is atomic — the companion is validated as part of the character publish, and any failure publishes nothing. Drafts use optimistic concurrency on a draft revision (FR-CAT-3).
**Consequences:** A character version never exists without a valid companion; stories always snapshot a consistent pair; concurrent edits fail loudly instead of overwriting. Test I-20.

---

### ADR-091 — Epistrel authorizes no end users: service authentication, attribution and ownership, and privacy per requested viewpoint
**Status:** Accepted. Supersedes the authorization parts of ADR-033, ADR-047, ADR-059, ADR-078, and ADR-088.
**Context:** Epistrel had grown a user-permission system — per-story roles (player, author/GM, operator), catalog editors, grants, shared visibility, and a `story_creator` permission — whose lifecycle was undefined: who grants and revokes, where principals and groups come from, what happens when an owner leaves. Every consumer already has its own users and permissions, so a second permission model inside Epistrel would be a second source of truth that can disagree with the game's.
**Decision:** (1) Epistrel authenticates only the calling service and holds no user roles, grants, groups, or permissions; the consumer decides who may call what, act as which character, see which entries, and request which viewpoint (FR-AUTH-1). Operator endpoints sit under `/admin` so a deployment can restrict them at its edge. (2) Epistrel runs behind the consumer, and its privacy guarantee is stated per request: output for a viewpoint contains only what that viewpoint may know (FR-AUTH-2). (3) Authority comes from the operation, not the caller: a directive is canon because it is a directive; render-trigger rules and `player_authority` stay as story rules (FR-AUTH-3). (4) Viewpoints stay fail-closed: defaults resolve only to a character, `omniscient` is returned only when named, private reads are omniscient reads, and stored results are bound to the viewpoint they were produced for (FR-RESP-12). (5) Requesting principals are opaque, recorded for attribution, and never decision inputs; every owned entity has one owner and any number of consumer-named relations, none interpreted or enforced (FR-AUTH-4..6, 8). (6) Catalog entries are served as a `card` (default) or `full` projection, carry descriptive labels, and have a consumer-defined listing status that gates nothing (FR-SCN-10, FR-CAT-11).
**Consequences:** No permission model to keep in sync with the game, and the catalog-permission lifecycle questions disappear. The cost is stated plainly: Epistrel cannot stop a consumer that requests the omniscient view on a player's behalf. Tests C-12, C-13, C-15, C-19, I-21, S-TRUST-BOUNDARY.

### ADR-092 — No MCP server
**Status:** Accepted. Supersedes the MCP part of ADR-016.
**Context:** The MCP server existed for "tool-using agents", but no use case needs it: Epistrel calls models directly, Orchestrator extensions cover in-engine automation, and an MCP surface handed to an end user's own model would need the per-user authorization Epistrel no longer has (ADR-091).
**Decision:** Epistrel exposes REST and the in-process library only (FR-API-2). A consumer that wants MCP access for its users — to create characters with their own model, or browse public entries — builds its own MCP server over the REST API with its own authorization (e.g., OAuth). Epistrel remains an MCP client of external-data providers (FR-EXT-1), whose content stays untrusted (FR-API-5).
**Consequences:** One fewer surface to secure and keep in parity, and no path by which an injected agent asks Epistrel directly for the omniscient view. Tests C-02, S-TRUST-BOUNDARY.

### ADR-093 — Principal offboarding works type by type
**Status:** Accepted. Builds on ADR-091.
**Context:** When a consumer deletes a user, it must find what that user owns and dispose of it by kind — typically deleting their stories while keeping their catalog work.
**Decision:** A lookup lists everything owned by or related to a principal, by type. One offboarding operation takes a per-type action — `delete`, `transfer` to a named principal, or `keep` — for stories, characters (companions follow), scenarios, personas, media, and engine profiles, plus `remove` or `keep` for relations; unnamed types are kept. Media used by transferred entries move with them. Deletion uses each type's normal semantics; media and profiles still referenced elsewhere are retired, not purged. The operation uses the warn-then-`ignore_warnings` flow (for example, deleting a story shared with other players) and runs as a resumable, idempotent job with a final report. It is not erasure: attribution on events in other principals' stories stays, and removing a person's content from those stories is a separate subject erasure (FR-AUTH-7).
**Consequences:** "Delete the user's stories, give their characters to a curator" is one call with a preview. Opaque principal IDs keep retained attribution from identifying anyone on its own. Test I-21.

---

### ADR-094 — Content ratings are deployment-defined guidance plus guardrails; a story's rating changes only on request
**Status:** Accepted. Builds on ADR-011 and ADR-091.
**Amended by:** ADR-098 — ratings carry separate image guidance, and a tightening check covers story images; ADR-107 — rating suggestions are labeled advisory, read by no enforcement path, and author-facing; ADR-132 — untruncate, swap, and fork promotion re-check reactivated turns against the guardrails in effect.
**Context:** Scenarios checked that member ratings did not exceed the scenario's, but no scale, ordering, or meaning was defined, so the check could not be implemented consistently, and nothing said whether a rating was a label or access control.
**Decision:** Ratings are deployment records — content-policy description, display order, prompt guidance, own guardrails, and configurable inheritance from any other ratings — seeded as Everyone, Teen, Mature, Explicit, each inheriting from the next less restrictive (FR-RATE-1,2). Every catalog character, scenario, and media asset has one; a story runs at its scenario's rating, and every character plays at the story's rating whatever its own (FR-RATE-3). Guidance goes into every generation prompt under every profile (FR-RATE-4). Ratings are compared by guardrail sets, never by order, so non-linear ratings work: mismatches warn and never block (FR-RATE-5). Only an explicit request changes a story's rating: relaxing applies at once without checks (FR-RATE-6); tightening applies only if the entire active history passes the stricter guardrails, and is otherwise refused (FR-RATE-7). A read-only suggestion returns one row per rating — appropriate or not, and the specific policy conflicts — judged on content policy alone (FR-RATE-8). A rating is not access control; who may play at which rating is the consumer's decision.
**Consequences:** The check the finding asked for is defined, and ratings now drive behavior rather than labels. Story content can never move a story to a looser rating. Rewriting a history to fit a stricter rating is deliberately left for later. Tests U-29, I-22, C-21.

### ADR-095 — Every character has a true age; a seeded global policy protects minors at every rating
**Status:** Accepted. Builds on ADR-011 and ADR-088.
**Amended by:** ADR-098 — the minors policy also applies to image prompts and outputs, with depicted characters' true ages stated in every prompt; ADR-108 — true ages go to the image safety path only; the image model gets apparent age bands.
**Context:** A looser scenario rating overrides a character's own, so a child character can be cast in an Explicit story, and nothing in the engine knew any character's age — only free-text appearance.
**Decision:** Every character has a true age from the moment it exists: declared for catalog characters, ad-hoc definitions, and personas; assigned lazily and plausibly for characters introduced in play, from the most specific clue available (FR-CAT-12). The age is canon, held by the engine, known to the character, and perceived by others only as an apparent band; current age follows the world clock. Policies may be conditioned on the ages of characters involved in or present for content (FR-GUARD-11). Epistrel seeds a global policy — no sexual content involving a minor, or in a minor's presence, at any rating — applied to input and output. It is data, not code: an administrator may remove it, the removal is recorded, and the documentation states it ships enabled and that removing it is the deployment's responsibility.
**Consequences:** The protection holds wherever a minor is cast, improvised, or played, including at Explicit, without forcing a fork of the project to change policy. Recognizing sexual content is model-assisted and measured; ages and presence are deterministic. Lite enforces the cast's ages but cannot age characters it does not track. Tests S-MINORS, S-AGE-ASSIGNMENT, R-07.

### ADR-096 — Global guardrails screen text coming into a story
**Status:** Accepted. Refines ADR-011.
**Context:** Guardrails were evaluated on outbound messages only, so content that breaks a global rule could enter the story through player input and shape everything after it, even when every reply was screened.
**Decision:** Every policy declares whether it applies to output, input, or both; global policies apply to input by default, and rating and story policies to output (FR-GUARD-8,10). Respond input, ingested events, directives, rewrite instructions, story text items, and creation-time personas, definitions, and seeds are checked before anything is recorded. A violation rejects the request with an error naming each policy so the user can rewrite; the text is not stored, and the audit keeps policy IDs only. In-story steering stays the response to everything else.
**Consequences:** Nothing that breaks a global rule enters a story from outside it. The minors-policy error necessarily reveals that a minor is involved or present, but never who. Test C-20.

---

### ADR-097 — Images are optional; characters carry a visual identity; large media are delivered in pieces
**Status:** Accepted. Supersedes the required primary image of ADR-081.
**Amended by:** ADR-108 — reference images and add-ons declare a perceivable or restricted audience.
**Context:** Requiring a primary image made image upload or an image-generation integration a prerequisite even for text-only or accessibility-focused deployments. Separately, video was stored, but nothing said how large files are served or uploaded.
**Decision:** Images are optional; a deployment setting `require_primary_image` (default off) requires one for visual products (FR-CAT-4). Characters may carry a versioned visual identity — image model, base appearance prompt, negative prompt, seed, reference images, opaque adapter parameters — used by Epistrel's generation and by consumers' own generators; scenarios may carry a visual style (FR-SCN-2). The limit is stated plainly: a seed reproduces one picture, while consistency across poses comes mainly from reference images. Media delivery supports byte ranges, signed URLs on S3, resumable uploads, and a poster image for every video, with no transcoding (FR-MEDIA-3).
**Consequences:** Text-only deployments need no image pipeline; visual ones opt in. Tests C-15, C-23.

### ADR-098 — Image generation is an optional core capability behind a port; image guardrails fail closed
**Status:** Accepted. Builds on ADR-083, ADR-094, and ADR-095.
**Amended by:** ADR-100 — images are staged until checked and jobs revalidate their origin before promotion; ADR-101 — image checking is a measured backstop with published error rates; ADR-102 — capabilities are discovered per endpoint, and metadata records which parameters were actually applied; ADR-104 — image limits count every provider output, and retries draw from them; ADR-108 — prompts carry apparent age bands, not true ages; ADR-109 — the rating assurance covers only images Epistrel checked; ADR-110 — Consequences restated in narrowed form.
**Context:** Consumers could already upload images, but each would have to build prompts that keep characters consistent and within the story's rating, and Epistrel holds the knowledge those prompts need. Text guardrails cannot see pixels.
**Decision:** An `ImageGenerator` port with one bundled adapter, for OpenRouter's image API — one interface to many image models, with seeds and reference images where the model supports them; more adapters can follow, and none is required, since consumers can generate and upload (FR-IMG-1). Epistrel builds prompts from visual identities, explicitly stated true ages *[superseded by ADR-108: apparent age bands; true ages go only to the safety path]*, the scenario's style, the rating's image guidance, and, for story images, only what the image's viewpoint perceived; a prompt-builder serves consumers that generate themselves (FR-IMG-2). Prompts are checked before generation and outputs by a vision model; if an image-output policy cannot be evaluated, the image is discarded rather than kept (FR-IMG-3). Generation runs as idempotent background jobs outside turn budgets, with per-story caps and cost reporting (FR-IMG-4). Video generation is not provided.
**Consequences:** Epistrel's prompts aim to keep images consistent with their characters and within the story's rating, but it vouches only for images it generated and checked (`passed`), and only to its checker's measured accuracy for each policy; uploads and `unchecked` images carry no rating assurance. Deployments that generate images through Epistrel need a vision model for image checks while the seeded minors policy is on. The bundled adapter is a hosted service; it is optional, and a self-hosted adapter (ComfyUI, for example) is the natural next one. Tests I-23, C-22. *[revised per ADR-101, ADR-105, ADR-109, and ADR-110]*

### ADR-099 — Story images are anchored at narrative positions, made for a viewpoint, and follow the timeline
**Status:** Accepted. Builds on ADR-028 and ADR-047.
**Amended by:** ADR-100 — a story image is attached only if its turn variant is still active; ADR-101 — the viewpoint guarantee covers the image model's inputs, not the pixels, and not uploads; ADR-103 — images declare their depicted subjects; ADR-105 — audience viewpoint is separate from camera POV, and metadata states each image's guarantees; ADR-110 — Consequences restated: privacy covers the image model's inputs only.
**Context:** Images inside a story need a place in it, must not show a player what their character never saw, and must behave like the surrounding text under rewinds, forks, and erasure.
**Decision:** A story image is a story record at a narrative position — after a beat, or at an inter-turn slot — made for one output viewpoint; history reads return it in place, only for that viewpoint (FR-IMG-5). It follows timeline membership: discarded with its turn, reactivated by a swap, copied by a fork; erasure removes images that depict or name the erased subject. Images come on request or, with `illustration_mode = auto`, at render-marked moments, capped per turn (FR-IMG-6). Catalog drafts can generate their own images (FR-IMG-7).
**Consequences:** Pictures sit where they belong and follow the same timeline rules as prose. Their privacy guarantee is narrower than prose's: it covers what the image model is given, not what it draws, and does not extend to uploaded or consumer-prompted images. Test I-23. *[revised per ADR-101, ADR-105, and ADR-110]*

---

### ADR-100 — Generated images are staged until checked, and image jobs are bound to their origin
**Status:** Accepted. Refines ADR-098 and ADR-099.
**Amended by:** ADR-104 — catalog results become candidates, attached by an explicit draft save; ADR-109 — "never stored unchecked" covers generated images; catalog candidates are durable private assets; ADR-110 — Consequences restated in narrowed form.
**Context:** The adapter stored returned images before they were checked, contradicting "never stored." And image jobs are asynchronous: a job could finish after its turn was truncated, swapped away, or regenerated, after a subject was erased, or after its catalog draft changed, was published, or was deleted.
**Decision:** Generated (and, with a checker, uploaded) bytes go to private staging, with no asset row and no URL, and are checked there; only an image that passes is promoted, and its asset row is created in the same transaction that attaches it. Rejected, failed, cancelled, and stale results are deleted, and a TTL sweep removes crash leftovers (FR-IMG-3). Each job records its origin: the story, operation, turn variant, viewpoint, and timeline revision, or the draft, its revision, and a fingerprint of its image inputs. Promotion revalidates all of it: the turn variant is still active, the story is not paused, current policies pass, the erasure ledger is clear, no relevant amendment has landed, or the draft's image inputs are unchanged. *[catalog part superseded by ADR-104: catalog results become candidates, and the fingerprint is checked when a candidate is attached]* A failed revalidation ends the job `stale` or `target_gone` with nothing attached (FR-IMG-4).
**Consequences:** No image Epistrel generates is addressable before it passes its checks — uploads may be stored `unchecked` when no checker is configured — and no story image lands in a timeline it no longer fits; catalog results become candidates that are attached only deliberately. Unrelated draft edits don't waste a generation. Test I-25. *[revised per ADR-104, ADR-109, and ADR-110]*

### ADR-101 — Story-image privacy is a guarantee about the image model's inputs; image checking is a measured backstop
**Status:** Accepted. Refines ADR-098 and ADR-099; follows ADR-077.
**Amended by:** ADR-105 — check status and viewpoint basis are returned with every image; ADR-110 — checker certification is reported per policy; ADR-108 — the image safety path knows true ages; the image model gets only what the audience perceives
**Context:** The documents said a story image "never depicts" what its viewpoint did not perceive, but only the prompt was filtered, an image model can ignore or embellish its prompt, uploads were unchecked, and the vision checker had only stub tests and no quality floor.
**Decision:** The privacy claim matches text privacy (NFR-PRIV-4). Everything the image model receives for a story image — prompt and reference images — comes only from what the viewpoint perceived. Characters of unknown identity get no visual identity or references, and the prompt passes the deterministic protected-content match. Images are not inspected for unperceived content, and consumer-prompted and uploaded images carry the consumer's guarantee only (FR-IMG-8). The prompt-side check is the primary safety control. The vision checker is a backstop measured on a labeled set with age-sensitive and adversarial cases, with false-negative and false-positive rates published per policy and model, and the minors policy gated under the observed-zero rule (NFR-QUAL-12). Uncertified checkers are labeled as such on every image *[per policy — ADR-110]*.
**Consequences:** Documents promise only what the engine enforces. Tests I-24, E-11.

---

### ADR-102 — The image adapter discovers capabilities per endpoint and records what it actually applied
**Status:** Accepted. Refines ADR-098.
**Amended by:** ADR-118 — adapters declare `confirms_completion` and return a provider request identifier with every call; ADR-131 — the bundled adapter is `sync`; the port separates `submit` from `collect`.
**Context:** OpenRouter routes one model to several provider endpoints, and its documentation lists supported parameters per endpoint. A model-level assumption that seeds or reference images are always honored would make the stored generation metadata claim reproducibility or consistency the image never had.
**Decision:** The adapter targets the dedicated `POST /api/v1/images` endpoint with base64 responses, and reads each endpoint's `supported_parameters` (cached with a TTL) instead of assuming capabilities per model. Requests carrying a seed or reference images are routed only to endpoints that support them, with fallbacks off; if none does, the image is generated without them and the metadata records which requested parameters were not applied, along with the serving endpoint and the actual output dimensions (FR-IMG-1, FR-IMG-4).
**Consequences:** Generation metadata is truthful about what can be reproduced; consistency features degrade visibly rather than silently. Test I-23.

---

### ADR-103 — Images declare who they depict; erasure reaches images only through declared or named subjects
**Status:** Accepted. Refines ADR-033 (erasure boundary) and ADR-099.
**Context:** Erasure promised to remove images that depict a subject, but nothing said who records whom an image shows, and a text index cannot find someone who is present only visually.
**Decision:** Every image carries depicted subjects: filled by Epistrel for images it generates, always including a catalog character in its own images, and required — explicitly empty allowed — for uploads and consumer-prompted generations (FR-IMG-9). Erasure reaches images through those subjects and through names in prompts, captions, and alt text. Epistrel does not recognize people in pixels, and the erasure boundary says so (FR-ADMIN-8).
**Consequences:** The erasure promise for images is one the engine can keep. A consumer that under-declares leaves images outside it. Test C-24.

### ADR-104 — Image limits count every provider output; catalog results are candidates attached deliberately
**Status:** Accepted. Refines ADR-098 and ADR-100.
**Amended by:** ADR-109 — reservations are atomic across all limits, and daily limits follow the deployment time zone; ADR-135 — concrete default limits; zero disables; no unlimited value; ADR-138 — limits are admission caps, not a spending cap
**Context:** Caps on "images per turn and per day" did not say whether guardrail retries, multi-image requests, or discarded outputs count, so a cap could be bypassed and retry cost was unpredictable. Catalog results attached themselves to a draft that might have moved on.
**Decision:** The limit unit is the output image: every image a provider returns counts, kept or not; a request for `n` reserves `n`; retries draw from the same limits; overruns end `cap_reached`. Cost covers every attempt, unbilled ones at zero (FR-IMG-11). Story images are one per request. Catalog results become candidates recording the draft revision and image-input fingerprint they came from, and are attached only by an explicit draft save that passes both checks; unattached candidates expire (FR-IMG-7).
**Consequences:** Limits and costs are predictable *[revised per ADR-135 and ADR-138: limits are admission caps; some costs are unknown, and an ambiguous send may be billed twice]*, and choosing among several portraits is a natural flow. Tests I-25, C-24.

### ADR-105 — Image metadata states its guarantees; an image's audience viewpoint is separate from its camera POV
**Status:** Accepted. Refines ADR-099 and ADR-101.
**Amended by:** ADR-110 — certification is reported per policy.
**Context:** Uploads may be stored `unchecked` while generated images fail closed, which readers could mistake for equivalent assurance; and "viewpoint" could be read as who may see an image or as where the camera stands.
**Decision:** Every image read returns its source, its check status (`passed` with per-policy results and checker certification *[reported per policy — ADR-110]*, or `unchecked`), and, for story images, its viewpoint basis; `unchecked` is never presented as passing, and no rating or viewpoint claim is made for unchecked or uploaded images (FR-IMG-10). A story image's **audience viewpoint** — who may receive it and whose perception its generated inputs come from — is a separate field from its **camera POV**, which is composition only and grants nothing (FR-IMG-5).
**Consequences:** Consumers can tell exactly what Epistrel vouches for, and framing an image from Bobby's eyes can never widen what Jenni's image is built from *[revised per ADR-110: the guarantee covers inputs, not pixels]*. Test C-24.

---

### ADR-106 — Creative quality is outside the guarantee; Epistrel guarantees the inputs
**Status:** Accepted. Refines ADR-077.
**Amended by:** ADR-112 — the engine's own interventions are held to a prose floor, and voice and plot are reported as signals.
**Context:** Quality floors measure engine correctness — leaks, entailment, extraction, time estimation — but none establish that a character stays in voice or that a story follows its scenario's hooks and planned reveals, and the documents did not say whether those were promised.
**Decision:** Keep the guarantee boundary narrow. Epistrel guarantees structurally that each character's definition reaches that character's context, that story drivers reach the director's context, and that planned reveals follow visibility. It does not guarantee, measure against a floor, or gate on voice fidelity or plot adherence; those are model- and prompt-dependent and application-level, and the documents say so (NFR-QUAL-13).
**Consequences:** No implied promise of creative quality. A deployment can add its own evals without changing the engine. Test C-25.

### ADR-107 — Review clarifications: persona text is never canon, one validation report shape, stale versus uncertified, advisory rating suggestions
**Status:** Accepted. Refines ADR-087, ADR-088, ADR-089, and ADR-094.
**Amended by:** ADR-141 — validation reports also use the outcomes `deferred` and `inactive`
**Context:** A review confirmed several designs but asked for their rules to be stated where future edits cannot erode them.
**Decision:** (1) A persona's description is player-provided and never objective canon: it yields only perceptions, acquaintances' revisable beliefs, and narration guidance; the name and true age are the only canonical persona fields (FR-CAT-6). (2) Validation, publish, and story creation share one report shape — code, location, exact text, outcome (`ignored`, `dropped`, `kept`) *[revised per ADR-141: reports also use `deferred` and `inactive`]*, prevailing source, and rule — so authors see which text was ignored and why (FR-SCN-5,7). (3) `uncertified` means no certification ever matched the fingerprint; `stale` means a matching certification was later invalidated by a canary failure or detected drift (FR-PROF-16). (4) Rating suggestions are labeled advisory, are read by no guardrail, publish, creation, or rating-change path, and are author-facing because they quote private content (FR-RATE-8).
**Consequences:** Each rule is now explicit in the requirements and covered by a test (U-27, C-18, C-19, C-21).

---

### ADR-108 — The image safety path knows true ages; the image model gets only what the audience perceives
**Status:** Accepted. Refines ADR-095, ADR-098, and ADR-101.
**Amended by:** ADR-114 — only unidentified figures are model-classified; known ages and presence are deterministic; ADR-129 — the safety path sends only what each check needs, and hosted checkers are outside the erasure boundary.
**Context:** Prompts were to state each depicted character's true age, while story-image inputs were to come only from what the audience viewpoint perceived — and a true age can be secret from that viewpoint. Age-conditioned checks also leaned on the declared subject list, which may legitimately be empty, and reference images could show details a viewpoint has never seen even when it knows who the character is.
**Decision:** Two data paths (FR-IMG-12). The safety path — prompt-policy evaluation and the image check — gets canonical true ages for every resolved subject: declared or builder-depicted subjects, characters named in the prompt, caption, or alt text (through aliases), and characters authoritatively present at the story position. A figure that resolves to no character is classified adult, minor, or indeterminate, and indeterminate is treated as a minor. The generation path gets only the audience's perception: apparent age bands, never true ages, and only visual-identity elements marked `perceivable`. Reference images and add-ons must declare `perceivable` or `restricted`, and restricted ones are used only for omniscient images (FR-CAT-4, FR-IMG-2,8). Catalog images are public, so they use only perceivable elements.
**Consequences:** Safety and privacy no longer compete for the same field. An empty declaration cannot hide a named minor, and an unidentifiable figure fails closed. Test I-24.

### ADR-109 — Catalog candidates are durable private assets; image reservations are atomic; unchecked uploads are named as such
**Status:** Accepted. Refines ADR-100 and ADR-104.
**Amended by:** ADR-113 — the general media sweep skips candidates; only the candidate-expiry sweep removes them; ADR-114 — provider attempts are fenced; reservations of stalled attempts are held until their horizon; ADR-117 — reservations, not usage, are what concurrent requests cannot push past a limit; ADR-135 — concrete default limits.
**Context:** Candidates were kept for seven days while promotion was tied to attachment, staging was swept hourly, and no job state said "candidates ready." Cap reservations could race. And several statements said nothing unchecked is ever stored or that images fit their ratings whichever side made them, although uploads may be stored `unchecked`.
**Decision:** A passing catalog output is promoted to durable storage with a media-asset row in state `candidate` and a candidate record. It is readable only through candidate endpoints (list, content, signed URL), the job ends `candidates_ready`, attachment flips it to `attached`, unattached candidates expire after `candidate_ttl`, and the staging sweep never sees them (FR-IMG-7). Every provider call is preceded by an all-or-nothing reservation transaction that locks the counters in a fixed order; reservations convert to usage on return and are released on termination or lease expiry *[superseded by ADR-114: a stalled or crashed attempt's reservation is held until its horizon (FR-IMG-11)]*; days follow the deployment's configured time zone (FR-IMG-11). "Fail closed" and "never stored unchecked" apply to images Epistrel generates; uploads may be stored `unchecked`, with no rating or viewpoint assurance from Epistrel (FR-IMG-3, FR-IMG-10).
**Consequences:** Each lifecycle state an implementer needs is named, concurrent requests cannot jointly reserve beyond a limit (usage can exceed a limit only through the overage named in ADR-116 and ADR-117), and every statement about checked images matches what is enforced. Tests I-25, I-26, C-22, C-24. *[revised per ADR-117]*

---

### ADR-110 — Checker certification is reported per policy; image tests and ADR consequences stay inside the guarantee boundary
**Status:** Accepted. Refines ADR-101 and ADR-105.
**Context:** An image's metadata reported one certification status for its checker, although certification is earned per policy (NFR-QUAL-12). A stub-based test was worded as if it showed what an image depicts, and earlier ADR consequences still claimed more than ADR-101, ADR-105, and ADR-109 allow.
**Decision:** Every checked image reports, per applicable policy, the result and whether its checker is certified for that policy; there is no blanket certification (FR-IMG-10). Image tests with stub generators are scoped to prompt inputs, parameters, metadata, and lifecycle; image content is measured only by the policy-check eval (E-11). The Consequences of ADR-098, ADR-099, and ADR-100 are restated in their narrowed form, and the log's conventions now require that for any narrowed consequence.
**Consequences:** A checker certified for the minors policy cannot be read as certified for a rating policy, and no test or ADR implies a pixel-level guarantee. Tests C-24, I-23.

---

### ADR-111 — Players can rate messages; feedback is a flag, never an input
**Status:** Accepted.
**Context:** Roleplay players expect to rate replies, and the people running a deployment need a signal for quality problems that engine metrics miss — and for real-world memory and guardrail misses.
**Decision:** A 1–5 rating per message variant per principal, with optional reason tags (seeded: out of character, repetitive, forgot something, factual error, ignored my action, too long, too short, should have been blocked, other; deployments may add) and optional free text, recorded with the configuration it was produced under (FR-FB-1). Feedback is audit-category: never in memory, canon, retrieval, or prompts, and it changes no behavior (FR-FB-2). It is aggregated with implicit signals from the log, and "should have been blocked" and memory-related tags feed review lists (FR-FB-3). Exporting rated messages as eval cases is a deployment opt-in, off by default (FR-FB-4).
**Consequences:** Quality problems become visible per profile and model without letting user input steer the engine. Feedback can later calibrate judges and seed eval sets. Tests C-26, C-27.

### ADR-112 — The engine must not degrade prose *[in aggregate, as an eval target — ADR-114]*; voice, plot, repetition, and length are reported, not gated
**Status:** Accepted. Refines ADR-106.
**Amended by:** ADR-114 — the prose floor is an aggregate eval target, not a per-message promise; ADR-115 — at least 300 non-tied pairs per intervention type, with confidence intervals reported.
**Context:** The quality floors covered facts and privacy, while repair, composition, and polish are engine steps that can make prose stiffer, and voice and plot quality were neither guaranteed nor measured.
**Decision:** One prose floor covers what the engine itself causes: wherever an engine step changed the first draft, a calibrated pairwise judge must not prefer the first draft on voice and readability in more than 55% of non-tied pairs, reported per intervention type (NFR-QUAL-14). Repetition and length accuracy (computed without a model) and judge-scored voice consistency and story-driver adherence are reported per configuration with no floors, alongside their correlation with user feedback (NFR-QUAL-15). The guarantee boundary of ADR-106 stands.
**Consequences:** Epistrel is measured on the prose it touches, as an aggregate target on its eval sets — not a promise that every repaired or composed message reads as well as its draft — without promising creative quality it doesn't control, and games get comparable numbers for choosing models and profiles. Tests E-12, E-13. *[revised per ADR-114]*

---

### ADR-113 — The general media sweep spares drafts and candidates; the images introduction separates the three sources
**Status:** Accepted. Refines ADR-083 and ADR-109.
**Context:** The general media sweep purged any asset that no catalog version or story referenced. That would delete catalog candidates, and images attached only to an unpublished draft, long before their intended lifetime. And the introduction to the images requirements still said Epistrel builds and checks the prompts for every image, uploads included.
**Decision:** Catalog drafts and live candidate records count as references; the general sweep skips assets in state `candidate`, which only the candidate-expiry sweep removes; content-addressed bytes are deleted only when no asset row of any state still uses them (FR-MEDIA-2). The images introduction now distinguishes Epistrel-prompted generation, consumer-prompted generation, and uploads, and what Epistrel does and does not vouch for in each (§18.7).
**Consequences:** No live image is swept early, and the introduction matches FR-IMG-3, FR-IMG-8, and FR-IMG-10. Test I-25.

---

### ADR-114 — Precise wording for age checks and the prose floor; image attempts are fenced
**Status:** Accepted. Refines ADR-108, ADR-109, and ADR-112.
**Amended by:** ADR-116 — fenced attempts settle their own reservation record; post-horizon results are bounded overage; ADR-117 — no-overlap holds before the horizon, and after it only with an adapter that confirms completion; ADR-125 — after the horizon, no-overlap holds only while a confirming job waits; ADR-115 — the prose floor needs enough evidence per intervention type
**Context:** FR-IMG-3 called the image age check deterministic, although unidentified figures are classified by a model. The prose floor was described as "must not read worse," although it is a 55% aggregate target. And a provider call could return after its worker's lease had expired and its reservation had been released, while a retry ran alongside it.
**Decision:** (1) Known subjects, true ages, and presence are resolved deterministically; only figures that resolve to no character are classified by a model, with indeterminate treated as a minor (FR-IMG-3, FR-GUARD-11). (2) NFR-QUAL-14 is an aggregate evaluation objective for the engine's own interventions, not a per-message promise, and there is no absolute floor for voice or plot. (3) Every provider attempt carries an attempt token checked by every promotion, attachment, and counter update. *[superseded in part by ADR-116: counter updates go through per-attempt reservation records, and a fenced token may settle its own record]* On lease expiry the reaper advances the token and holds the attempt's reservation until its horizon (start + adapter timeout + grace); no new attempt for the job starts before then *[after the horizon, see ADR-117]*. A late result counts as usage, records its cost, is never promoted, and is reported `late_discarded`; no result by the horizon releases the reservation, with cost `unknown` unless the adapter can retrieve it (FR-IMG-11).
**Consequences:** The documents claim exactly what is enforced or measured. Before an attempt's horizon, one job never has two billable calls in flight; after it, a retry may overlap a late call unless the adapter confirms completion — and even then once `confirm_wait_max` has passed — and late or extra outputs are counted as reported overage. Reservations never jointly exceed a limit; usage can exceed a limit only through that overage. Tests I-24, I-26, E-12, C-25. *[revised per ADR-116, ADR-117, and ADR-125]*

---

### ADR-115 — The prose floor needs enough evidence per intervention type
**Status:** Accepted. Refines ADR-112 and ADR-114.
**Context:** A rarely triggered repair rung could appear to pass the 55% prose floor on a handful of comparisons.
**Decision:** Each intervention type needs at least 300 non-tied pairs to pass, and passes when the mean across seeds plus one standard deviation is at most 55% (NFR-QUAL-8). Every report gives the rate with its 95% confidence interval. Types below the minimum are `insufficient_evidence`, never passed, and certifications list the types the floor covers. Eval suites include scenarios that trigger each rung. Judge calibration uses at least 200 labeled pairs, reported with its interval (NFR-QUAL-14).
**Consequences:** No intervention type is certified on thin evidence, and sparse rungs are visibly uncovered rather than silently passing. Test E-12.

---

### ADR-116 — Fenced image attempts settle their own reservation through a dedicated path
**Status:** Accepted. Refines ADR-114.
**Amended by:** ADR-117 — extra outputs beyond `n` are overage; retries after the horizon may overlap a late call; ADR-119 — results retrieved by the current owner through `status` are adopted; duplicates settle as no-ops; ADR-121 — counter fields and their aggregation are defined exactly; ADR-124 — reservations snapshot their counters; provider delivery is at least once
**Context:** A fenced worker could not update counters, yet its late outputs had to count as usage, and nothing said what happened to a result arriving after its reservation was released — so retry and cap accounting could diverge.
**Decision:** Each provider attempt has its own reservation record (held, settled, released), and the limit counters are the sums of those records. Promotion and attachment require the job's current token. A fenced token may only settle its own attempt, through an idempotent settlement path keyed by the attempt. Before the horizon, that converts the held reservation into usage. After the horizon, it records the outputs as overage against the released record and reports `late_after_horizon`. Overage can exceed a limit only by an abandoned attempt's outputs *[superseded in part by ADR-117: extra outputs beyond the requested `n` are overage too]*, after which new reservations in the period are refused (FR-IMG-11). FR-MEDIA-2's deletion rule now names drafts and live candidates alongside catalog versions and stories.
**Consequences:** Every billed output is counted exactly once, accounting never depends on a fenced worker's ability to touch shared counters, and post-horizon overage is limited to outputs a provider actually returned beyond their reservation, and reported. Tests I-24, I-26. *[revised per ADR-117]*

---

### ADR-117 — After an attempt's horizon, retries wait only when the adapter can confirm completion; overage is the only way past a limit
**Status:** Accepted. Refines ADR-114 and ADR-116.
**Amended by:** ADR-118 — what counts as definitive completion, and the `confirm_wait_max` fallback; ADR-125 — no-overlap lasts only until `confirm_wait_max`; the fallback may overlap; ADR-120 — adopted outputs after the horizon are overage
**Context:** The documents promised both that one job never has two provider calls running and that a late result arriving after its horizon is settled as overage. Both cannot hold when a retry starts at the horizon and the old call is still running. "Concurrent requests can never jointly exceed a limit" also overstated the guarantee, and nothing covered a provider returning more images than requested.
**Decision:** Before an attempt's horizon, no retry starts. After it, an adapter that can confirm completion or cancel a call is asked first, and the retry waits until the old call is confirmed finished or cancelled *[revised per ADR-118 and ADR-125: or until `confirm_wait_max`, after which the fallback may overlap the old call]*. For an adapter that cannot — the default assumption — the retry may start at the horizon, and a late result is settled as overage without touching the retry. Reservations never jointly exceed a limit. Usage can exceed a limit only through overage: post-horizon late results and extra outputs beyond the requested `n`, which are discarded unchecked, counted, and flagged `extra_outputs` (FR-IMG-11).
**Consequences:** The no-overlap promise is exact: it holds before the horizon, and after it only with a confirming adapter and only while the job waits for a terminal status; once `confirm_wait_max` passes, the fallback retry may overlap the old call, whose late output is counted as overage. Every way past a limit is named and reported. Test I-26. *[revised per ADR-125]*

---

### ADR-118 — `confirms_completion` is a declared adapter capability with a strict meaning of "definitive"
**Status:** Accepted. Refines ADR-102 and ADR-117.
**Amended by:** ADR-119 — `cancel` is required for confirming adapters; a confirmed success is adopted; ADR-124 — adapters may declare `idempotency_keys`; ambiguous sends are recovered or presumed; ADR-125 — the confirming wait is the only no-overlap window after the horizon; ADR-131 — `confirms_completion` requires `async` submission; the bundled adapter is `sync`.
**Context:** ADR-117 let retries wait for adapters that can confirm a call's completion, but the port and requirements did not say how an adapter declares or implements that. An accepted cancellation request does not mean the provider has stopped processing or billing.
**Decision:** The `ImageGenerator` port returns the provider's request identifier with every call and exposes `capabilities()` *[revised per ADR-131: an `async` adapter returns it on acknowledgement, before the results; a `sync` adapter only with them]*. An adapter declaring `confirms_completion` must implement `status(attempt_ref)`, which reports `running`, `succeeded`, `failed`, `cancelled`, or `unknown`, and may implement `cancel`. *[superseded in part by ADR-119: `cancel` is required and may return `not_supported`]* Only a terminal status reported by the provider for that request is definitive. An accepted cancel is not, and `unknown` never is. A confirming job requests cancellation if it can, polls status, and retries only on a terminal state; with none by `confirm_wait_max`, it falls back to the non-confirming rule and reports it (FR-IMG-1, FR-IMG-11). The bundled OpenRouter adapter declares the capability only if OpenRouter's image API is verified to support per-request terminal status *[superseded by ADR-131: the bundled adapter is `sync` and does not declare it]*. ADR wording elsewhere now says usage can exceed "a limit", and ADR-116's overage bound is marked as extended by ADR-117.
**Consequences:** The no-overlap path after a call's horizon rests on provider-confirmed terminal states only and lasts at most `confirm_wait_max`; after that the fallback may overlap the old call, so a confirming adapter can never stall a job forever. Tests I-23, I-26. *[revised per ADR-125]*

---

### ADR-119 — A confirmed success is adopted, not retried; `cancel` is part of the confirming contract
**Status:** Accepted. Refines ADR-116 and ADR-118.
**Amended by:** ADR-120 — adopted outputs after the horizon count as overage; outputs beyond `n` are discarded unchecked.
**Context:** `status` could report `succeeded` with the call's outputs, while late results were "never promoted", and nothing said whether to discard those outputs and retry, how to avoid counting them twice when the original call also returned, or where their cost came from. The port listed `cancel` as part of the interface while ADR-118 called it optional.
**Decision:** A result returned to a fenced worker is never promoted. A result the job's current owner retrieves through `status` is **adopted**: the old attempt's reservation record is settled once, and the outputs go through the normal checks, revalidation, and promotion under the current token, with no retry. `failed` or `cancelled` settles the record and retries. Settlement is idempotent per attempt, so a later return of the same outputs to the fenced worker changes nothing except filling in a still-unknown cost; it is reported `late_duplicate`. Status reports usage and cost when the provider supplies them, and otherwise the cost is `unknown`. Adapters declaring `confirms_completion` must implement both `status` and `cancel`; `cancel` may answer `not_supported`, and neither of its answers is definitive (FR-IMG-1, FR-IMG-11).
**Consequences:** A completed generation is never paid for twice or thrown away for being late, outputs are counted once per attempt, and the port and requirements agree. Test I-26.

---

### ADR-120 — Adopted outputs after the horizon are overage, and status outputs beyond `n` are discarded unchecked
**Status:** Accepted. Refines ADR-117 and ADR-119.
**Amended by:** ADR-121 — the record field is `requested`; counter sums are defined by state; ADR-125 — adoption applies only during the confirming wait; ADR-129 — the overage definition is restated in ADR-121's exact form.
**Context:** The success path adopted a confirmed call's outputs and counted them as usage, while the general settlement rule treats outputs against a released reservation as overage. Confirmation runs after the horizon, so the two rules met without saying which applied. The success path also did not say what happens to a status response with more outputs than requested.
**Decision:** Adopted outputs are settled against the old attempt's already-released record, so they count as usage and as overage *[revised per ADR-125: adoption happens only while the job still waits for a terminal status; after the `confirm_wait_max` fallback, the old call's output is a late result — overage, never adopted]*. Overage is an accounting category only: outputs within the requested `n` still go through checks, revalidation, and possible promotion. Outputs beyond `n` are discarded unchecked and unpromoted and flagged `extra_outputs`. Counter fields are defined so that `used` includes overage and, for an attempt that settles with *m* outputs, `overage = m − covered`, where `covered = min(m, requested)` if its reservation record was `held` when it settled and 0 if it had already been `released`; `extra_outputs = max(0, m − requested)`, which is always part of overage (FR-IMG-11). *[restated in ADR-121's exact form per ADR-129]*
**Consequences:** Every path through settlement uses the same accounting, and I-26 asserts the exact counter values for adoption after the horizon and for an over-full status response. Test I-26.

---

### ADR-121 — Image limit counters are defined sums over per-attempt reservation records
**Status:** Accepted. Refines ADR-116 and ADR-120.
**Amended by:** ADR-124 — each reservation snapshots the (scope, key, period) it charges; ADR-130 — a presumed settlement is corrected once by the actual result; ADR-129 — one overage formula
**Context:** The counters were described as "the sums of" the reservation records, but nothing said which records each field sums. A settled record kept its reserved amount while the aggregate's `reserved` did not change, and `overage` appeared in tests but not in the counter schema.
**Decision:** Each reservation record has a fixed `requested`, plus `used`, `overage`, `extra_outputs`, and a state. On settlement with *m* outputs: `used = m`; `overage = m − covered`, where `covered = min(m, requested)` if the record was held and 0 if it was released; `extra_outputs = max(0, m − requested)`. Counters per scope and period: `reserved` sums `requested` over held records; `used`, `overage`, and `extra_outputs` sum over settled records. Admission requires `used + reserved + k ≤ limit`. Counters are stored and updated in the same transaction as each record change; records are the source of truth, and a reconciler reports drift (FR-IMG-11).
**Consequences:** Schema, limit checks, and test assertions use the same arithmetic, and any counter can be recomputed and audited from the records. Test I-26.

---

### ADR-122 — Compaction is an explicit, irreversible control operation
**Status:** Accepted. Refines ADR-018 and ADR-080.
**Context:** Undo, swap, and promoting a dormant continuation depend on discarded and dormant records, yet optional compaction could garbage-collect them, and FR-CONC-15 listed compaction as a "prospective" setting although it deletes existing history.
**Decision:** Compaction happens only through a compaction operation, a two-stage control action. The impact report lists what will be deleted and which undo, swap, and promotion operations will stop being possible, and nothing is deleted without `ignore_warnings`. It deletes only discarded tails and dormant continuations — never active records, amendments, or what an amendment's undo needs — and never outcome records, outbox rows, reconciliation listings, erasure markers, or feedback rows. It leaves markers, bumps the timeline revision, and makes the post-compaction log the basis for replay, as erasure does. Afterwards, undo, swap, or promotion that needs compacted records is refused with `compacted`. The compaction *policy* is prospective: changing it deletes nothing by itself, and opting into automatic runs is the consumer's confirmation (FR-STORE-32).
**Consequences:** Reversible history stays reversible unless the consumer explicitly gives part of it up, and the classification is consistent. Test S-COMPACTION.

### ADR-123 — Safety calls are reserved by formula; a turn that cannot fit them is planned smaller or rejected
**Status:** Accepted. Refines ADR-080.
**Amended by:** ADR-126 — a fixed `primary` is deferred, never narrated reactively; ADR-133 — G counts a policy once per text it checks; O is reserved at planning when a turn can become staged.
**Context:** The default budget (3 + 4 per primary) omitted claim detection on the composed message and model-assisted guardrail checks, the number of beats was unbounded, and nothing said what happens when mandatory safety calls alone exceed the budget.
**Decision:** The default budget is 4 + 5 per primary character + 1 per model-assisted guardrail policy on the turn's input or output *[revised per ADR-133: 4 + 5 per primary + G + O, where G counts a policy once per text it checks and O reserves a possible staged turn's outcome step]*. A turn reserves S = B + C + G + O: an extraction per planned beat, composed-message detection (released for deterministic assembly), the model-assisted guardrail checks, and staged outcome steps. A repair starts only if its re-render and extraction can both be reserved. Planning reduces primaries to reactive narration or deferral until renders + S fit. If one primary still doesn't fit, the turn is rejected before any model call with `budget_below_safety_minimum`, and configuration validation warns in advance. The budget therefore also bounds the number of beats (NFR-PERF-6,7).
**Consequences:** No beat is ever released without its detection, no budget silently starves safety, and misconfiguration fails loudly before cost is incurred. Test P-09.

### ADR-124 — Image reservations snapshot their counters; provider delivery is at least once
**Status:** Accepted. Refines ADR-116, ADR-118, and ADR-121.
**Amended by:** ADR-130 — presumed settlements are provisional; ADR-131 — `async` adapters commit the provider identifier before collecting results.
**Context:** The reservation row's `scopes` field was undefined, so a late result, a midnight boundary, a deleted target, or a regenerated turn could leave settlement unsure which counters to charge. And a crash after a provider accepted a request, but before Epistrel recorded its identifier, could lead to a second billed attempt with the first one's usage never recorded.
**Decision:** Each reservation stores an immutable list of (scope, scope key, period) captured at creation. Settlement, release, and reconciliation always use that snapshot, and accounting rows outlive their story or entry until settled *[and for the accounting retention — ADR-130]*. Every attempt is recorded as `sending`, with a provider idempotency key, before the call is made. An ambiguous send is recovered by re-sending with the same key when the adapter declares `idempotency_keys`. Otherwise the attempt is settled at its horizon as presumed usage, with cost unknown *[provisionally — the actual result, if it ever arrives, corrects it once; ADR-130]*, and may be retried — at-least-once delivery, possibly billed twice — and every ambiguous send is reported. Promotion stays at most once (FR-IMG-1, FR-IMG-11).
**Consequences:** Accounting is unambiguous across midnight, deletion, and regeneration; limits err toward generating less; and possible double billing is visible rather than silent. Test I-26.

---

### ADR-125 — After the horizon, no-overlap holds only while a confirming job waits
**Status:** Accepted. Refines ADR-114, ADR-117, ADR-118, and ADR-120.
**Context:** For confirming adapters the documents said that polling until a terminal status makes overlap impossible, and also that after `confirm_wait_max` the job falls back to the non-confirming rule and may retry while the old call is still running. Both rules are intended, but the unqualified guarantee contradicted the fallback.
**Decision:** The guarantee is qualified everywhere it appears: before an attempt's horizon no retry starts; after it, a job whose adapter confirms completion does not retry — so nothing overlaps — while it waits for a terminal status. When `confirm_wait_max` passes, the job stops polling, falls back, and reports the fallback; from then on its retry may overlap the old call. The old call's output, if it ever returns, is a late result — settled as overage, reported `late_after_horizon`, and never adopted or promoted. Adoption (ADR-119, ADR-120) applies only during the wait (FR-IMG-11).
**Consequences:** No document claims more than the wait guarantees, and late output after a timeout follows the one overage rule. Test I-26.

### ADR-126 — A fixed participation role is a bound; escalation that cannot happen is deferred
**Status:** Accepted. Refines ADR-031 and ADR-123.
**Context:** FR-MULTI-7 lets authors fix background characters as "only ever reactive", while FR-MULTI-10 escalated any reactive character whose reaction needed speech, a decision, or a State change. It was undefined which rule wins, and also what happens when the budget cannot fit an escalation.
**Decision:** A per-character role is either a default, which escalation and budget planning may change for a turn, or fixed, which the engine never crosses on its own. Escalation never raises a fixed `reactive` or `silent` character, and budget planning never lowers a fixed `primary` (it defers it). Only the request changes a fixed role — by naming the character or overriding its role. When escalation is unavailable (`fixed_role`) or does not fit the budget (`budget`), the reaction stays within reactive bounds. Nothing more happens for that character this turn: no speech beyond an allowed interjection, no decision, hidden-fact commitment, or State change, and no other render may narrate one for them. Undetermined values stay open, and the response lists the character in `escalation_deferred` with the reason; it is not an error (FR-MULTI-7,10, NFR-PERF-7).
**Consequences:** Authors can rely on "only ever reactive", the game learns when a moment is waiting on a character and can bring them in next turn, and the budget gap in escalation is closed. Tests S-REACTIVE, REACTIVE-BOUNDS.

### ADR-127 — Workers poll durably and own every timer; no external scheduler
**Status:** Accepted. Refines ADR-002.
**Context:** The architecture named `LISTEN/NOTIFY` with `SKIP LOCKED` and an optional scheduler (APScheduler/arq) for timed work, so a missed notification or a restart could strand jobs. Required lifecycle work — reservation horizons, staging cleanup, candidate expiry, and others — had no defined owner in the default deployment.
**Decision:** Every job and timed action is a durable row with a due time, and notifications only shorten latency. Workers also poll (default every 5 s), hold renewable leases, and run a recovery pass on start-up. Epistrel's own workers own every timed lifecycle job through built-in timer and recurring-task tables, claimed with `SKIP LOCKED` so each run happens once; every timed action is idempotent. Overdue work runs at the next start-up, late but never skipped. The default image runs the API and workers together; a health endpoint reports `degraded` when no worker is alive or timers fall behind. No external scheduler, cron, or broker is used (NFR-REL-5, NFR-OPS-1,2).
**Consequences:** A missed wake-up costs one polling interval, a restart loses nothing, and a deployment without workers is visibly degraded rather than silently leaking reservations and staged files. Test X-07.

### ADR-128 — Stories export as versioned bundles and import only as new stories
**Status:** Accepted. Refines ADR-019 and ADR-046.
**Amended by:** ADR-132 — imported inactive turns are checked if they are ever reactivated; ADR-143 — bundles carry the story's audit events and feedback history, never deployment audit logs; import limits fail with `bundle_too_large`; ADR-144 — bundles match a rating by name
**Context:** Complete-story export and import (FR-STORE-7) and snapshot/restore (FR-ADMIN-4) were required and tested, but no API operation, bundle format, or conflict behavior was defined, and FR-STORE-7 had no test.
**Decision:** Export writes one versioned archive of a story at one revision. It contains a manifest with checksums, the full event log with markers, stored derived outputs, catalog snapshots, settings, feedback, and media — never projections, vectors, or deployment-level accounting *[revised per ADR-143: the event log includes the story's audit-category events within retention, and feedback includes its rating history; the deployment's own audit and operational logs are never exported]*. Import always creates a new story, so it cannot conflict with or overwrite an existing one; restoring a story means importing its bundle. Import treats the bundle as untrusted *[revised per ADR-143: size, entry-count, and per-entry limits are deployment settings, and exceeding one is `bundle_too_large`; a refusal at any stage leaves nothing]*. Before the story is served, it applies this deployment's erasure ledger, checks the whole active history against the chosen rating's guardrails as a tightening does, and attaches an engine profile under the usual impact and `ignore_warnings` rules. Pending outbox rows are parked for reconciliation and never dispatched. Any refusal leaves nothing behind. Deployment-level backup stays `pg_dump` plus media, with the ledger reapplied on restore. Both operations live under `/admin`. FR-STORE-7 is now a must (FR-STORE-7, FR-ADMIN-4).
**Consequences:** Stories are portable between deployments without becoming a path around erasure, guardrails, or the minors policy. An import can never repeat an external effect, and the self-contained story model (ADR-019) makes the copy simple. Test I-10.

---

### ADR-129 — Review clarifications: release gating for S and C requirements, one overage formula, and the image checker's data path
**Status:** Accepted. Refines ADR-108, ADR-120, and ADR-121.
**Amended by:** ADR-136 — the data-flow table is generated from the purpose registry.
**Context:** A traceability review found five S requirements with no named test, and FR-STORE-19 credited to a truncation test that never exercises compaction. ADR-120 still described overage as "the part not covered by a held reservation", while ADR-121 gives the exact formula. It also noted that the image checker, which receives true ages and images, is itself a model provider, and the documents did not describe that data path or its retention boundary.
**Decision:** (1) Release gating: every M requirement needs a passing test; an S or C requirement with a named test is gated by it; one without is listed in the traceability appendix as not release-gated. The five S requirements now have tests — resolution levels (FR-COMMIT-9), context assembly by tier (FR-RET-7), story inspection (FR-ADMIN-5), per-turn call and cost tracking (FR-OBS-3, in P-09), and a documentation check for capability floors (FR-MODEL-10, in C-04). I-04 no longer claims FR-STORE-19, which S-COMPACTION covers. (2) ADR-120's overage definition is restated in ADR-121's form: `overage = m − covered`, with `covered = min(m, requested)` for a record held at settlement and 0 for one already released. (3) The safety path's model calls send only the prompt, caption, and alt text, the policy text, opaque subject labels with true ages and the prompt's visual descriptors, and, for the output check, the image — never story text, beliefs, other canon, or names the prompt lacks. The documentation publishes a data-flow table per model purpose and states that a hosted checker keeps what it receives on its provider's terms, outside the erasure boundary; self-hosted checkers keep it in-house (FR-IMG-13, FR-ADMIN-8).
**Consequences:** Traceability is accurate and every requirement's gating status is explicit; the decision history has one overage definition; operators can see, and choose, where true ages and images go. Tests U-30, U-31, C-28, C-04, P-09, I-24.

---

### ADR-130 — A presumed image settlement is provisional and is corrected once by the actual result
**Status:** Accepted. Refines ADR-121 and ADR-124.
**Context:** An ambiguous send from an adapter without idempotency keys was settled at its horizon as presumed usage, while a late result from an abandoned attempt was to be settled as overage against a released record. Those are different states, and idempotent settlement forbade a second settlement anyway, so a real result arriving after a presumption had no defined accounting: replace, add, fill in cost only, or ignore.
**Decision:** The presumption settles the record while it is still held, so its outputs count as covered. If the actual result later reaches the settlement path, the record is corrected once to what a held settlement with *m* outputs would have recorded — `used = m`, `overage = extra_outputs = max(0, m − requested)`, the result's cost — and the snapshot's counters move by the difference. Its basis becomes `corrected`. The outputs are never adopted or promoted, the retry and any finished job stand, and the attempt is reported `late_after_presumed`; any further delivery is `late_duplicate`. A result that arrives before the horizon settles normally, and the attempt is never presumed. Accounting rows are kept for an accounting retention (default 90 days) after their period closes, so late results and corrections find them; a result after that is reported `late_unaccounted` with its cost recorded (FR-IMG-11).
**Consequences:** When the actual result is ever seen, usage ends at its true value; no sequence counts an output twice, and the provisional state is visible. Test I-26.

### ADR-131 — The image port separates submission from collection; adapters declare `async` or `sync`
**Status:** Accepted. Refines ADR-102, ADR-118, and ADR-124.
**Context:** The port's single `generate(...) → {attempt_ref, outputs}` returned the provider reference only with the outputs, so the engine could not persist it before waiting, although the confirmation and cancellation path and the ambiguous-send tests depend on having it while a call runs.
**Decision:** The port is `submit(request, idempotency_key) → {attempt_ref, outputs?}`, `collect(attempt_ref)`, `capabilities()`, and, for confirming adapters, `status` and `cancel`. An adapter declares its submission mode. With `async`, the provider acknowledges with an identifier before generating; Epistrel commits it before calling `collect`, so a crash after acknowledgement leaves a known request — recovered through `status` by a confirming adapter, otherwise held to its horizon as a stalled attempt — and only the moment between sending and committing the acknowledgement is ambiguous. With `sync`, the identifier comes only with the results, so the whole call is the ambiguity window, recovered by idempotency key or presumption. `confirms_completion` requires `async` submission and lookup from any worker; registration rejects a `sync` adapter that declares it. The bundled OpenRouter adapter is `sync`, because its endpoint returns the images in the response, and does not declare `confirms_completion` (FR-IMG-1, FR-IMG-11).
**Consequences:** Each adapter's guarantees follow from what it declares, and recording the provider reference before waiting is possible wherever the provider allows it. Test I-26.

### ADR-132 — Reactivated turns are re-checked against the guardrails in effect, including inactive turns from an import
**Status:** Accepted. Refines ADR-094 and ADR-128.
**Context:** Imports check only active history, while bundles carry truncated tails and dormant continuations. The re-check on reactivation was stated only for turns discarded before a tightening, so a disallowed branch imported inactive could seemingly be swapped or untruncated back without a check.
**Decision:** Every turn variant records the guardrail set it last passed. Untruncate, swap, and promoting a dormant continuation into a fork first check, as the FR-RATE-7 background job, every turn they would reactivate whose recorded set lacks a policy now in effect, with its story images, and are refused with the violations listed if any fails. Turns already checked against the current set are not checked again. Imported inactive turns carry no passed set, so they are checked if they are ever reactivated; the same rule covers a tightening and a newly added policy (FR-RATE-7, FR-STORE-7).
**Consequences:** No path — tightening, a new policy, or an import — brings content into the active story without passing the guardrails now in effect, and imports stay affordable because only content that comes back is checked. Tests I-10, I-22.

### ADR-133 — Guardrail calls are counted per text checked; a possible staged turn reserves its outcome step at planning
**Status:** Accepted. Refines ADR-123.
**Context:** The budget added one call per model-assisted policy "that applies to the turn's input or output", leaving a policy that applies to both ambiguous. The outcome-step reservation O was not in the default formula, and it was not said whether it raised the minimum, displaced optional work, or could cause rejection.
**Decision:** A model-assisted policy costs one call per text it checks. G = one call per model-assisted input policy, plus one per model-assisted output policy for each output checkpoint, so a policy on both input and output is two calls. An ordinary turn has one output checkpoint, its released message. A staged turn has two: its intent beats, checked before the intent commits, and its completed message. Whether a turn becomes staged is known only once a render requests an effect, so in any story with a write-capable `synchronous` provider, O is reserved at planning. O is fixed: the outcome render, its extraction, and the second checkpoint's output-policy calls. O is part of the safety minimum — it can shrink the plan or cause rejection — and is released for optional work once every render has finished without requesting such an effect. The default budget is 4 + 5 per primary + G + O. A repair triggered by an output policy must also reserve that output's re-checks, or the safe fallback is used (NFR-PERF-6, FR-EXT-16).
**Consequences:** Every turn's minimum is computable before its first call and is pinned by exact-boundary tests, and no staged turn can run out of calls to narrate and check its outcome. Test P-09.

---

### ADR-134 — Delivery runs in seven milestones, each gating the requirements it delivers
**Status:** Accepted. Builds on ADR-084.
**Amended by:** ADR-138 — tests gate per milestone part; requirements spanning milestones are split; ADR-139 — M0 infrastructure and M1 project setup and CI come first; the milestones here are now M2–M8; ADR-141 — genesis is staged by capability across milestones; ADR-144 — each milestone depends on the one before it; releases follow the numbering
**Context:** The specification declares the complete capability, and the release gate required every M requirement at once — profiles, epistemic memory, time travel, external effects, catalog, ratings, images, import and export, and worker recovery — so the first useful integration would have arrived only at the end.
**Decision:** Keep the full specification, and build it in seven independently testable milestones: M1 Lite core, M2 catalog and product surface, M3 structured memory and verification, M4 character knowledge and multi-character play, M5 the world (States, clock, external data), M6 history, profiles, and portability, and M7 images *[renumbered by ADR-139: these are now M2–M8, after M0 infrastructure and M1 project setup and CI]*. Every requirement is assigned to exactly one milestone in a table in the specification (§22), and the testing plan's appendix shows that milestone for each requirement. Milestone *k*'s gate is every M requirement assigned to milestones 1..*k*, over every capability delivered so far; cross-cutting requirements gate from the milestone that introduces them and extend as capabilities land. M1's exclusions are explicit, and because erasure arrives in M2, no release that stores real players' data may precede M2 *[renumbered by ADR-139: these are M2 and M3 — erasure arrives in M3, and no release with real players' data may precede it]*. The milestones follow the capability map (FR-PROF-13), so each one is a coherent engine profile, not a slice through modules.
**Consequences:** A game can integrate against M1 *[milestone numbers in this ADR predate ADR-139; add one to each]*, the riskiest subsystems — verification, then privacy — are proven in M3 and M4 before more is built on them, and the full release gate is unchanged. Tests: the milestone gates in *04 §13*.

### ADR-135 — Image limits have concrete defaults; zero disables; nothing is unlimited
**Status:** Accepted. Refines ADR-104 and ADR-109.
**Amended by:** ADR-138 — the limits are admission caps, not a spend ceiling.
**Context:** Image limits were "configurable with defaults", but no default values were given, so a default installation had no predictable cost ceiling or contention behavior.
**Decision:** The defaults are 4 output images per turn, 40 per story per day, 16 per catalog entry per day, and 200 per deployment per day, with four candidates per catalog request. Every limit is a whole number: 0 disables generation at its scope (`images_disabled`), negative or missing values fail configuration validation, and there is no unlimited value. The deployment and entry limits are deployment settings; the per-turn and per-story limits are story settings defaulted from the deployment. Without a configured adapter, generation returns `not_configured` (FR-IMG-11).
**Consequences:** Every deployment that can generate images caps the outputs it admits per day, and operators can switch generation off anywhere by setting a limit to zero. The limits are not a spending cap: reported overage can exceed them, some costs are unknown, and an ambiguous send may be billed twice. Test I-26. *[revised per ADR-138]*

### ADR-136 — One purpose registry generates the model guide; prompts and payloads use only declared inputs
**Status:** Accepted. Refines ADR-007 and ADR-129.
**Amended by:** ADR-138 — the guardrail check declares the visual descriptors too.
**Context:** The documents required a published data-flow table for every model purpose, but no such table existed, nothing owned it, and nothing kept it from drifting away from the actual prompts and adapter payloads. Writing the design-level table also exposed a stale architecture row that still listed true ages as an input to the image-prompt model.
**Decision:** Every purpose, the embedding port, and the image adapter are declared in one registry in code, with a typed schema of the inputs their requests may carry, each tagged with its content category. Templates and payload builders can use only declared fields; anything else fails the build or a contract test. The model guide — capability floors and the data-flow table, with hosted-provider retention notes — is generated from the registry, is a deliverable of every release, and is owned by the Model Access Layer maintainers; CI fails when the published guide differs. The architecture carries the design-level table until the registry exists, and its image-prompt row now lists apparent age bands, not true ages (FR-MODEL-13, FR-IMG-13).
**Consequences:** The data-flow table cannot silently drift from what is actually sent, and a prompt cannot quietly start carrying a new category of data. Test C-04.

### ADR-137 — Capacity claims stop at what has been measured
**Status:** Accepted. Refines ADR-002.
**Amended by:** ADR-145 — the concurrency target is defined by a reference host and workload
**Context:** ADR-002 put the ceiling "well above hobby scale (~10M vectors)", while the gated vector-scale test stops at 100k memories and the deployment target is a few concurrent stories. Nothing measured or described the larger claim.
**Decision:** Capacity is claimed only for the measured range (NFR-SCALE-1, P-06), and the dev/hobby target (NFR-PERF-4) is stated separately. Larger figures are labeled unvalidated estimates. An informational capacity benchmark — never a release gate — extends the curve to 5M memories. It records index build time and memory, write throughput, and retrieval and commit latency, and reports the first budget that fails. ADR-002's consequence is restated accordingly.
**Consequences:** The documents claim only what is measured, and any larger claim must cite the benchmark that measured it. Test P-06, plus the informational capacity benchmark.

---

### ADR-138 — Milestone gates are scoped per test part; requirements that spanned milestones are split
**Status:** Accepted. Refines ADR-084, ADR-134, ADR-135, and ADR-136.
**Amended by:** ADR-139 — milestone numbers here predate the renumbering; add one; ADR-140 — parts are also checked against a capability lexicon.
**Context:** Under ADR-134, several M1 requirements were covered only by broad tests that also exercised later capabilities. FR-ADMIN-1 relied on I-10's export and import round trip, FR-STORE-1 on erasure and compaction scenarios, and NFR-PERF-6 on P-09's staged turns. FR-PROF-6 reached into data-producing changes that need M6's backfill. So no milestone's acceptance rule could be applied consistently. The whitepaper also called the image limits a ceiling on the bill, and the design-level data-flow row for the guardrail check omitted the visual descriptors that FR-IMG-13 requires.
**Decision:** (1) Every test gates from exactly one milestone. A test whose assertions span milestones is divided into milestone parts, each marked with its milestone and the requirements it covers; a part may exercise only capabilities delivered by then. A test without parts gates from the latest milestone among its requirements. CI checks that every part references only requirements assigned at or before its milestone, and that every M requirement has a test or part gating at or before its own milestone; the traceability appendix lists each test with its gating milestone. Some 30 broad tests are now divided into parts. (2) Requirements that spanned milestones are split or moved. FR-ADMIN-1 now covers story lifecycle only, since catalog and scenario lifecycles have their own requirements. FR-PROF-6 covers following the latest version for behavioral settings, and the new FR-PROF-18 (M6) covers data-producing increases and decreases. FR-RATE-3,5 and FR-CAT-7 move to M2, FR-STORE-31 to M4, FR-API-5 and NFR-SEC-2 to M5, and FR-VERIFY-14 to M3 *[milestone numbers in this ADR predate ADR-139; add one to each]*. (3) The image limits are described as admission caps, not a spend ceiling. (4) The guardrail check's data flow includes the prompt's visual descriptors, and I-24 asserts that the registry, the generated guide, and the captured payloads agree.
**Consequences:** Each milestone's gate is computable and depends on nothing later. Broad end-to-end tests remain, gating from the milestone that completes them. Cost language promises only what the counters enforce. Tests: the milestone checks in *04 §13* and §15; I-24.

---

### ADR-139 — Infrastructure and project setup come first as M0 and M1; delivery milestones become M2–M8
**Status:** Accepted. Refines ADR-134 and ADR-138.
**Amended by:** ADR-140 — FR-SCN-8 moves to M5 and FR-EXT-8 to M7; ADR-143 — M4 routes nothing by perception; perception arrives in M5; ADR-144 — milestone dependencies are sequential, and some requirements move to M2 and M3
**Context:** The milestone plan began with the Lite core as M1, leaving no place in the sequence for standing up infrastructure or for setting up the project and its CI, both of which must exist before any requirement can be built or gated.
**Decision:** Two milestones precede delivery: **M0 — Infrastructure** and **M1 — Project setup and CI**. Both are not yet planned: they carry no requirements and have no gates defined until they are planned. Every delivery milestone moves up by one — the Lite core is M2, the catalog M3, structured memory M4, character knowledge M5, the world M6, history and profiles M7, and images M8 — with the same scope, assignments, dependencies, and exclusions. The Lite core now depends on M1. Every requirement assignment, test part marker, and milestone reference in the specification and testing plan is renumbered accordingly. Milestone numbers in ADR-134 and ADR-138 predate this change and are marked inline (03 §22).
**Consequences:** The plan has a place for infrastructure and CI work ahead of the first requirement-bearing milestone, and nothing else in it changes. M8's gate is the full release gate.

---

### ADR-140 — Test parts are checked against a capability lexicon; the remaining cross-milestone tests are split
**Status:** Accepted. Refines ADR-138 and ADR-139.
**Amended by:** ADR-141 — genesis is staged by capability
**Context:** Several tests without milestone parts still asserted capabilities from later milestones that their requirement references did not reveal. I-18 used scenario snapshots and a paced clock; S-DELETE included subject erasure; S-TRUST-BOUNDARY required erasure and history rewrites; R-07 used directives and rewrites; S-MINORS relied on a time skip; and C-02's first part listed erasure and offboarding endpoints. The CI checks compared only requirement IDs, so they could pass while a part's assertions exceeded its milestone.
**Decision:** (1) The testing plan carries a **capability lexicon**: the terms each milestone introduces (scenario, erasure, consolidation, belief, State, clock, amendment, rewrite, image generation, and so on). CI fails any part — or any test without parts — whose text uses a term from a later milestone; test titles are not checked, and a short list of phrases naming test infrastructure or an absence is excluded. The lexicon is a guard, not a proof, so review still confirms each part. (2) Every test the lexicon flagged is split, about 55 in all, including the six above. I-18's first part covers inline-character Lite behavior, its scenario snapshots gate at M3, and its prompt-only clock at M6. S-DELETE's subject erasure gates at M3. C-02 lists each `/admin` endpoint with the milestone that delivers it, because endpoints appear with their capability and are never inert placeholders. (3) Two requirements whose subject belongs later move: FR-SCN-8 (epistemic seeding by holder and audience) to M5, and FR-EXT-8 (directives cannot override external values) to M7 (03 §22).
**Consequences:** A milestone's gate can no longer fail for work it does not deliver, and implementers are not led to build later capabilities early. New tests must keep their parts' text inside the lexicon. Tests: the lexicon check in *04 §13*.

---

### ADR-141 — Scenario fields have classes, and genesis materializes them by capability
**Status:** Accepted. Refines ADR-082, ADR-088, ADR-134, and ADR-140.
**Amended by:** ADR-142 — Lite genesis detects no fact conflicts; per-class handling, card-projection reports, and forward-only seed visibility are defined.
**Context:** M3 delivers story creation from scenarios, but genesis was specified to create partitions, beliefs, seeded canon, predicates, States, the clock, and scheduled events in one atomic operation — capabilities that arrive in M4–M6. Scenario validation likewise required predicate normalization, an M4 capability. The specification also said every private scenario field "is seeded as fact at genesis", although director guidance, hooks, settings, recommendations, and visual style are instructions, configuration, or metadata, not facts.
**Decision:** (1) Every scenario field belongs to one class, fixed in the schema: public card, knowledge seeds, story rules, starting state, director guidance, story configuration, engine recommendation, or style. Each class has one storage place and one channel. Only knowledge seeds become story truth; director guidance reaches only the director (or Lite's author notes) and never supports or contradicts a claim; style reaches only image prompts. (2) Genesis is capability-driven, not milestone-driven. It always snapshots everything, creates the story, its configuration, and its characters, and records the opening. It materializes each class only as far as the story's effective capabilities use it, and defers the rest *[revised per ADR-142: only knowledge seeds, story rules, and starting state are deferrable; the other classes are applied, consumed, or stored `inactive`]*. Deferred content stays in the snapshot and is listed in the creation report as `deferred` with the capability it needs — never dropped and never written as a placeholder. Settings for missing capabilities are listed `inactive`. A later data-producing profile change materializes deferred content at position 0, resolving conflicts with play through the backfill policy (FR-SCN-9,11, FR-PROF-18) *[revised per ADR-142: under `forward_only`, conflicts with earlier play are not detected, and the impact report says so]*. (3) Validation runs the checks the deployment supports, lists the rest as `check_unavailable`, and re-runs at story creation for the capabilities the story uses (FR-SCN-5). (4) Each milestone from M4 to M7 extends genesis for what it delivers, and the M3 deliverable is defined as Lite genesis with deferral.
**Consequences:** M3 can deliver scenario-based story creation on its own, the same code path serves Lite and structured stories at every milestone, and guidance and configuration can never leak into fact storage or retrieval. Test: the new genesis-by-capability test.

---

### ADR-142 — Lite genesis does not detect fact conflicts; per-class handling, report projections, and forward-only seeds are explicit
**Status:** Accepted. Refines ADR-087, ADR-088, and ADR-141.
**Context:** Four gaps remained after ADR-141. U-27's M3 part expected a role seed to defeat a conflicting character fact, which needs M4's contradiction rules. The architecture said that "everything else" in a Lite story was deferred, which swept in fields that are applied or consumed at creation, and it listed a creation-rule class the requirements do not have. The creation report's deferred and `check_unavailable` entries had no defined form under the default `card` projection. And it was unclear whether seeds materialized by a `forward_only` upgrade are visible at earlier checkpoints.
**Decision:** (1) Fact-conflict detection needs the structured pipeline. Under `lite`, genesis drops nothing: the Lite prompt renders scenario facts ahead of character facts, with an instruction that scenario facts prevail, and the fact-merge check is reported as `check_unavailable`. The merge runs when the structured form is materialized. Personality precedence applies under every profile (FR-SCN-7). (2) The architecture enumerates each class's handling under Lite and structured stories. Only knowledge seeds, story rules, and starting state are deferrable; configuration is applied, or stored `inactive`; director guidance and recommendations are consumed; public-card fields and style have nothing to activate. Creation rules are `configuration` fields with use `creation_only`, not a separate class (FR-SCN-11). (3) Under `card`, story creation returns counts of deferred and inactive items by class and capability and the names of unavailable checks, quoting no private field. `full` returns every report entry with its location and text (FR-SCN-10). (4) Deferred authored seeds are inserted at position 0 under both upgrade modes, with the upgrade's transaction-time. They are part of the corrected history at every checkpoint, are absent from earlier as-originally-recorded views, and are used by verification from then on; earlier turns are not re-verified. Under `forward_only`, conflicts with earlier play are not detected, and the impact report says so (FR-PROF-18).
**Consequences:** M3's gate asks nothing of M4, the implementation has one table for genesis handling, players never see private fields in a creation response, and a forward-only upgrade's effect on history is predictable. Tests U-27, I-27.

---

### ADR-143 — M4 routes nothing by perception; bundles carry the story's audit events and enforce explicit limits
**Status:** Accepted. Refines ADR-128 and ADR-139.
**Context:** The M4 milestone claimed co-location perception, but perception requirements (FR-TURN-2, 9, 10) are assigned to M5, and with shared character knowledge there is nothing to route. FR-STORE-7 excluded "audit logs" from a bundle while the architecture keeps the story's audit-category events in its event log and the round-trip guarantee covers the as-originally-recorded view, which shows them. I-10 did not test malformed bundles, exceeded limits, or the round trip of feedback.
**Decision:** (1) M4 delivers the structured pipeline with shared character knowledge only: every character draws on one knowledge base and nothing is routed by perception. Perception — co-location and full routing — arrives with partitions in M5 (03 §22). (2) A bundle's event log includes the story's audit-category events still within retention — rejected renders, verification verdicts, repair attempts, rewrite questions and answers, and guardrail records with policy IDs only — and feedback travels with its rating history. The deployment's own audit logs (such as edits to global and rating policies, and feedback exports) and operational logs are never exported. Imported audit events keep their original recording times, so the target's retention counts from those times. The round-trip guarantee covers the as-originally-recorded view with its audit records, and message feedback (FR-STORE-7). (3) Import limits on total uncompressed size, entry count, and per-entry size are deployment settings with documented defaults, checked while decompressing; exceeding one is refused with `bundle_too_large`, naming the limit. Malformed content — an unreadable archive, an invalid manifest, a record that fails the bundle schema — is `corrupt_bundle`. A refusal at any stage leaves no story behind (FR-STORE-7).
**Consequences:** M4's description matches its assignments. A restored story audits exactly as the source did, without carrying deployment-wide records into another deployment. Oversized and hostile bundles fail fast with a specific error. Test I-10.

---

### ADR-144 — An exhaustive consistency pass: a complete capability map, perception levels, explicit pauses, and milestones released in order
**Status:** Accepted. Refines ADR-084, ADR-128, ADR-134, and ADR-139.
**Amended by:** ADR-145 — the M2 contract carries a channel filter; the audience filter is FR-RESP-13 at M5
**Context:** A full review of the five documents found requirements that applied under no stated module, profile dials that could switch off mechanisms the requirements still made mandatory, a perception setting whose levels no requirement defined, states and outcomes used but never defined (a paused story, an asynchronous effect's turn, best-effort rewrite resolution, rewrite severity), early milestones that depended on later ones, and a whitepaper that stated profile-scoped guarantees as universal.
**Decision:** (1) **Capability map.** FR-PROF-13 now places every requirement area: the `structured` pipeline (the turn contract, bitemporal canon, propositions, retrieval, embeddings, memory tiers, composition, directives, rewrites, read sets, and memory guardrails), per-character partitions (with the nested-belief, lazy-resolution, and testimony dials each defining what happens when off), perception, verification at its level, commitment, the clock, States and external data (prompt-only clock and States under `lite`, no providers), per-character renders, and consolidation; everything else is the floor. Structured-field repair applies wherever the field is required. Every module except the summary, pins, prompt-only clock and States, budgets, and models requires the `structured` pipeline, and the clock there requires commitment (FR-PROF-4). (2) **Perception levels** are `none` (the only level without partitions), `co_location` (every co-located character perceives every beat in full — a whisper is heard by the room), and `full` (the envelope/content split and qualifiers). The clock dial is off or on; its mode is a story setting, and `interaction_mode` stays a story setting (FR-PROF-2). (3) **Actors.** Input for a player's own character is authoritative; input for a character the platform plays is checked like its render; a catalog character is never a player's own character (FR-AUTH-3, FR-CAT-6, FR-VERIFY-11). (4) **Effects and pauses.** A turn with only asynchronous effects is ordinary; only a synchronous effect makes a staged turn, whose statuses include `abandoned`. A claim-time cancellation is narrated in a staged turn and recorded and listed for an asynchronous effect. A paused story refuses truth-changing calls with `story_paused`, serves reads, and settles arriving outcomes after the pause (FR-EXT-16, FR-CONC-5,16). (5) **Undefined terms defined:** ingest positions, operation kinds, the revision-changing operations, the regenerate boundary `(N−1)⁺k`, an expected-revision mismatch, `best_effort` as least-change resolution, and rewrite severity as `high` when the player has seen what would change (FR-RESP-6,10, FR-STORE-12,24, FR-CONC-1,4, FR-REWRITE-3). (6) **Immutability** has a third exception: audit records, feedback included, expire at the end of their retention without changing story truth (FR-STORE-1). **Models and priorities.** `image check` has no default model; a `belief adjudication` purpose is added; FR-EXT-10, FR-MODEL-10, and FR-STORE-19 are musts because musts depend on them (FR-MODEL-2,3). (7) **Author approval** for over-cap amounts and longer skips is a later directive or State API call; the turn repairs to the cap (FR-STATE-10, FR-CLOCK-8). (8) **Milestones.** Each milestone depends on the one before it, because gates are cumulative; work may overlap, releases may not. The output-viewpoint contract (FR-RESP-12) moves to M2, and profile schema, validation, the Lite preset, quality status, and catalog images (FR-PROF-2,3,4,16, FR-CAT-4) to M3. Until M7, a data-producing increase for a story with history is `upgrade_unavailable` and applies only to stories without history, tested by a part marked *interim* that M7 retires (FR-PROF-7, 03 §22). (9) **Erasure** names its scope — one story (with its story images), or the catalog and media — and keeps a claimed effect's reconciliation listing, redacted (FR-ADMIN-2, FR-EXT-21). Bundles match a rating by name. (10) The architecture bundles no AGPL BM25 extension.
**Consequences:** Every requirement has a stated scope, every dial's off position has defined behavior, and no milestone depends on a later one. The whitepaper scopes each guarantee to the profiles that provide it. Tests U-16, U-28, C-04, C-12, C-14, C-16, C-18, C-23, C-28, I-20, X-07, S-AGE-ASSIGNMENT, and S-THUNK-RESOLVE carry new parts; S-WHISPER gains a co-location variant.

---

### ADR-145 — The output viewpoint's channel filter applies everywhere, its audience filter only with perception; a reference workload for concurrency
**Status:** Accepted. Refines ADR-047, ADR-137, and ADR-144.
**Amended by:** ADR-146 — the concurrency workload is open-loop and staged by capability
**Context:** FR-RESP-12 required a perception-based audience filter on every response, but ADR-144 moved the output-viewpoint contract to M2, perception arrives in M5, and the capability map enables the filter only at `co_location` or `full`. What a character viewpoint receives with perception `none` — every Lite story, and structured stories with shared knowledge in M4 — was undefined, and M4's composition check referred to filtered beats before any filter existed. Separately, the concurrency target named "10+ concurrent stories on a commodity host" with no host or workload, so it could not be reproduced, and the architecture's image prompt builder named "the authoritative location" and "the present characters" without restating that both are as the viewpoint perceived them.
**Decision:** (1) FR-RESP-12 keeps the M2 contract — explicit viewpoint, fail-closed default, `omniscient` only when named, private reads omniscient, stored results bound to their viewpoint — plus a **channel filter** under every profile: a character viewpoint never receives `think` or `latent`. The **audience filter** becomes FR-RESP-13 (M5) and applies at `co_location` (a beat in full or omitted) and `full` (content, envelope, or nothing). With perception `none`, a character viewpoint receives every other beat in full, and the guarantee table says no audience filtering applies; composition is checked against those beats (FR-RESP-8). (2) NFR-PERF-4 defines a **reference host** (4 vCPU, 16 GB, NVMe, Postgres and workers co-located) and a **reference workload**: 10 stories of 10,000 memories and 20,000 events, each submitting its next turn 15 s after the last completes, for 30 minutes, against a stub model at a fixed 2 s per call, run with Lite stories and with structured two-character turns. Model time is excluded. It passes when the engine-only p95 budgets hold, nothing fails or is lost or duplicated, and submission-to-first-model-call is p95 ≤ 1 s; other hosts are reported, not gated. *[superseded by ADR-146: the workload is open-loop on a fixed schedule with queued turns, in Lite, structured, and multi-character variants staged by milestone, each gated only on the budgets that apply to it]* (3) Story-image inputs use the viewpoint character's location and the present characters as that viewpoint perceived them, never hidden world state (FR-IMG-8).
**Consequences:** Every milestone has defined viewpoint output, M4's composition check has a defined input, the concurrency gate can be reproduced, and the image prompt builder cannot be read as permitting hidden state. Tests C-12, P-04, and I-24.

---

### ADR-146 — The concurrency gate is a fixed-schedule capacity test, staged by capability
**Status:** Accepted. Refines ADR-145.
**Amended by:** ADR-147 — service criteria hold per story, with a maximum backlog and queue wait
**Context:** ADR-145's reference workload had two flaws. Its M2 run gated Lite stories on retrieval and deterministic-verify budgets (NFR-PERF-1,2) that Lite never exercises and that arrive in M4, and it gave Lite stories "10,000 memories" without saying what that means for a pipeline with pins and a rolling summary but no episodic retrieval. And each story submitted its next turn 15 s after its previous one completed — a closed loop in which a slower engine receives fewer requests, so the offered load was not fixed and the run could not measure capacity.
**Decision:** The gate measures capacity. Arrivals are open-loop: each story submits turns on a fixed schedule with `turn_concurrency = queue`, so a slow engine builds a backlog rather than slowing arrivals. Three variants run as their capabilities are delivered: **Lite** (M2) — 20,000-event stories, meaning a 10,000-message transcript with its summary segments, plus 50 pinned memories, a turn every 15 s; **structured** (M4) — 10,000 memories and 20,000 events, single-character turns every 60 s; **multi-character** (M5) — two-character turns with per-character renders every 60 s. Each variant gates on the engine-only budgets that apply to what it runs — turn commit always, retrieval and deterministic verify for structured stories — and on at least 99% of offered turns completed, no queue growing over the last 10 minutes, no failed requests, no lost or duplicated turns, and queue-front-to-first-model-call p95 ≤ 1 s. *[revised per ADR-147: completion, backlog, and queue-wait criteria hold for each story separately, with a maximum backlog and queue wait]* The host, run length, stub latency, and the exclusion of model time are unchanged (NFR-PERF-4).
**Consequences:** Each milestone's performance gate tests only what that milestone delivers, and a regression shows up as backlog and missed throughput instead of hiding behind slower arrivals. Test P-04.

---

### ADR-147 — The concurrency gate's service criteria hold per story
**Status:** Accepted. Refines ADR-146.
**Context:** ADR-146 gated on at least 99% of offered turns completing and no queue growing over the last 10 minutes. Both are aggregate: one story could fall far behind while the total stayed above 99%, and "not growing" set no limit on a stable but long backlog or queue wait. In roleplay each story is a player waiting on a reply, so per-story service is what matters.
**Decision:** Every service criterion of NFR-PERF-4 holds for each story separately. Each story completes every turn submitted at least 2 minutes before the run ends; after a 5-minute warm-up, it never has more than one turn waiting behind the one in progress; no turn waits longer than one schedule interval (15 s for Lite, 60 s for structured) to reach the front of its queue; and each turn's first model call starts within 1 s of reaching the front, at p95. The engine-only latency budgets stay aggregate p95 across all stories. P-04 reports every measure per story and in aggregate.
**Consequences:** A run fails if any single story falls behind, even when the aggregate keeps up, and the gate bounds both backlog and waiting time. Test P-04.

---

*Every ADR above is Accepted as a record; the indexes at the top show which are amended or partly superseded. No decisions are open.*

---

*See also: 01 — Whitepaper · 02 — Technology & Architecture · 03 — Requirements Specification · 04 — Testing Plan.*
