<!-- Generated from src/epistrel/orchestrator/models/registry.py by `uv run python -m epistrel.tools.model_guide --write`. Do not edit by hand: CI fails when this file differs from what the registry generates (FR-MODEL-13). -->

# Model guide

Every model call Epistrel makes names a **purpose**. This guide is generated from the purpose
registry, so it always matches what the code sends. For each purpose delivered so far it gives
the recommended **capability floor** (FR-MODEL-10) and the **data-flow row**: every input field
a request for that purpose may carry, with its content category, and the categories it never
receives (FR-MODEL-13). A prompt can carry only fields its purpose declares. A template that
references anything else fails the build, and so does a payload that adds a field.

Speaking the OpenAI-compatible protocol does not make a model capable for a purpose. A model is
capable for a purpose only when it meets that purpose's floor (NFR-PORT-3). The measured results
per model tier are in [model-matrix.md](model-matrix.md).

## Purposes delivered

| Purpose | Hot path | Default model | Structured output | Minimum context |
|---|---|---|---|---|
| `generation` | yes | `default` | `none` | 8,192 tokens |
| `guardrail_check` | yes | `default` | `parse_validate_retry` | 8,192 tokens |
| `consolidation` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `lite_summary` | no | `consolidation` | `none` | 8,192 tokens |
| `rating_suggestion` | no | `guardrail_check` | `parse_validate_retry` | 8,192 tokens |
| `quality_judge` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `importance_scoring` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `entailment` | yes | `default` | `parse_validate_retry` | 8,192 tokens |
| `salience_detection` | yes | `default` | `parse_validate_retry` | 8,192 tokens |
| `extraction_backstop` | yes | `default` | `parse_validate_retry` | 8,192 tokens |
| `regeneration` | yes | `generation` | `parse_validate_retry` | 8,192 tokens |

### `generation`

Writes the reply of a Lite turn from the story's prompt sections; in a structured story (#150) the typed beats of one character's or the narrator's render (a `response_schema`: constrained decoding or parse-validate-retry), the age repair and the composition of the turn's verified beats into its message (#152).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `lite_generation.j2`; variants: structured `structured_generation.j2`, narrator `narrator_generation.j2`, age_repair `age_repair.j2`, composition `composition.j2`
- **Capability floor:** structured output: not required; minimum context 8,192 tokens
- **Returns:** free text

**Receives**

| Field | Content category |
|---|---|
| `rating_guidance` | `rating_guidance` |
| `custom_instructions` | `custom_instructions` |
| `scenario_text` | `scenario_text` |
| `character_definitions.name` | `character_definition` |
| `character_definitions.definition` | `character_definition` |
| `character_definitions.deflection_style` | `character_definition` |
| `director_notes` | `director_notes` |
| `pinned_memories` | `pinned_memory` |
| `summary_segments` | `rolling_summary` |
| `recent_messages.name` | `recent_message` |
| `recent_messages.say` | `recent_message` |
| `recent_messages.do` | `recent_message` |
| `recent_messages.text` | `recent_message` |
| `recent_messages.role` | `recent_message` |
| `voiced_characters` | `character_definition` |
| `policy_constraints` | `policy_text` |
| `resolution_steering` | `constraint` |
| `player_input.name` | `player_input` |
| `player_input.say` | `player_input` |
| `player_input.do` | `player_input` |
| `scene` | `scene` |
| `working_memory` | `working_memory` |
| `retrieved_memories` | `retrieved_memories` |
| `canon_facts` | `canon_facts` |
| `open_references` | `scene` |
| `introductions.name` | `scene` |
| `introductions.clue` | `scene` |
| `composition_beats.index` | `verified_beat` |
| `composition_beats.kind` | `verified_beat` |
| `composition_beats.name` | `verified_beat` |
| `composition_beats.text` | `verified_beat` |
| `composition_draft` | `verified_beat` |
| `composition_omniscient` | `verified_beat` |

**Never receives:** `subject_true_age`, `checked_text`, `eval_text`

### `guardrail_check`

Checks one text against one model-assisted policy (FR-GUARD-9).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `guardrail_check.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `GuardrailVerdict` (`violation`, `confidence`), validated strictly; no free-text field

**Receives**

| Field | Content category |
|---|---|
| `text` | `checked_text` |
| `policy_prompt` | `policy_text` |
| `subjects.label` | `policy_text` |
| `subjects.age` | `subject_true_age` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `consolidation`

Compresses one window of episodic memories (and the canon in it) into standing facts and reflections, in the background (FR-MEM-3,6,9,10).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `consolidation.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `ConsolidationResult` (`facts`, `reflections`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `records.index` | `record_meta` |
| `records.text` | `memory_text` |
| `records.kind` | `record_meta` |
| `records.holder` | `record_meta` |
| `records.visibility` | `record_meta` |
| `records.position` | `record_meta` |
| `records.speaker` | `record_meta` |
| `scope` | `scope_meta` |
| `level` | `scope_meta` |
| `covers` | `scope_meta` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `lite_summary`

Folds old messages into a Lite story's rolling summary (FR-LITE-2).

- **Hot path:** no
- **Default model:** the `consolidation` row of `model_config`, then the `default` row, per field, unless the purpose has its own
- **Template:** `lite_summary.j2`
- **Capability floor:** structured output: not required; minimum context 8,192 tokens
- **Returns:** free text

**Receives**

| Field | Content category |
|---|---|
| `messages.name` | `recent_message` |
| `messages.say` | `recent_message` |
| `messages.do` | `recent_message` |
| `messages.text` | `recent_message` |
| `messages.role` | `recent_message` |
| `prior_segments` | `rolling_summary` |
| `character_names` | `character_definition` |
| `mode` | `rolling_summary` |
| `merge_segments` | `rolling_summary` |
| `covers` | `rolling_summary` |
| `target_tokens` | `rolling_summary` |

**Never receives:** `policy_text`, `subject_true_age`, `checked_text`, `eval_text`

### `rating_suggestion`

Reviews one catalog entry against one rating's content policy: its description, guidance and model-assisted policies (FR-RATE-8).

- **Hot path:** no
- **Default model:** the `guardrail_check` row of `model_config`, then the `default` row, per field, unless the purpose has its own
- **Template:** `rating_suggestion.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `RatingSuggestionVerdict` (`concerns`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `rating_name` | `rating_description` |
| `rating_description` | `rating_description` |
| `rating_guidance` | `rating_guidance` |
| `policies.policy_id` | `policy_text` |
| `policies.name` | `policy_text` |
| `policies.prompt` | `policy_text` |
| `deterministic_hits.policy_id` | `policy_text` |
| `deterministic_hits.kind` | `catalog_text` |
| `deterministic_hits.location` | `catalog_text` |
| `subjects.label` | `policy_text` |
| `subjects.age` | `subject_true_age` |
| `elements.kind` | `catalog_text` |
| `elements.location` | `catalog_text` |
| `elements.text` | `catalog_text` |

**Never receives:** `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `checked_text`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `quality_judge`

Pairwise prose comparisons and single-message scores for the eval harness (NFR-QUAL-14,15; #158): never in a turn, never routed by a profile.

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `quality_judge.j2`; variants: score `quality_judge_score.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `QualityJudgeVerdict` (`preferred`, `reasons`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `pair.a` | `eval_text` |
| `pair.b` | `eval_text` |
| `message` | `eval_text` |
| `reference` | `eval_text` |
| `criteria` | `eval_text` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `importance_scoring`

Scores one memory's importance, 0 (trivia) to 1 (pivotal) (FR-MEM-7).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `importance_scoring.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `ImportanceScore` (`importance`), validated strictly; no free-text field

**Receives**

| Field | Content category |
|---|---|
| `text` | `memory_text` |
| `kind` | `memory_text` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `entailment`

Judges the fuzzy residual of verify: one claim the deterministic check left uncertain, against the holder's records only (FR-VERIFY-3,8).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `entailment.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `EntailmentVerdict` (`verdict`, `confidence`), validated strictly; no free-text field

**Receives**

| Field | Content category |
|---|---|
| `claim` | `claim_text` |
| `records.text` | `canon_record` |
| `records.holder` | `canon_record` |
| `records.valid_time` | `canon_record` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `salience_detection`

Tells content placed to be noticed from background texture in one render's beats, naming the proposition each salient span raises (FR-COMMIT-3).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `salience.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `SalienceResult` (`salient`, `texture`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `beats.index` | `render_text` |
| `beats.kind` | `render_text` |
| `beats.speaker` | `render_text` |
| `beats.text` | `render_text` |
| `scene_context.present_entities` | `scene` |
| `scene_context.last_beats` | `scene` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `extraction_backstop`

The model layer of claim detection: lists every factual claim one render's beats, or the composed message, make, so a claim the declared structure and the deterministic detectors missed is still checked (FR-VERIFY-13,14).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `extraction.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `ExtractionResult` (`claims`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `beats.index` | `render_text` |
| `beats.kind` | `render_text` |
| `beats.speaker` | `render_text` |
| `beats.text` | `render_text` |
| `scene_context.present_entities` | `scene` |
| `scene_context.last_beats` | `scene` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

### `regeneration`

Rewrites one beat of a structured render that contradicted canon or asserted what its speaker may not introduce, under the repair's constraints, with the facts it contradicted and their neighborhood (FR-REPAIR-1,2; the repair ladder's rung 3).

- **Hot path:** yes
- **Default model:** the `generation` row of `model_config`, then the `default` row, per field, unless the purpose has its own
- **Template:** `regeneration.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** free text

**Receives**

| Field | Content category |
|---|---|
| `rating_guidance` | `rating_guidance` |
| `custom_instructions` | `custom_instructions` |
| `scenario_text` | `scenario_text` |
| `character_definitions.name` | `character_definition` |
| `character_definitions.definition` | `character_definition` |
| `character_definitions.deflection_style` | `character_definition` |
| `director_notes` | `director_notes` |
| `pinned_memories` | `pinned_memory` |
| `summary_segments` | `rolling_summary` |
| `recent_messages.name` | `recent_message` |
| `recent_messages.say` | `recent_message` |
| `recent_messages.do` | `recent_message` |
| `recent_messages.text` | `recent_message` |
| `recent_messages.role` | `recent_message` |
| `voiced_characters` | `character_definition` |
| `policy_constraints` | `policy_text` |
| `resolution_steering` | `constraint` |
| `player_input.name` | `player_input` |
| `player_input.say` | `player_input` |
| `player_input.do` | `player_input` |
| `scene` | `scene` |
| `working_memory` | `working_memory` |
| `retrieved_memories` | `retrieved_memories` |
| `canon_facts` | `canon_facts` |
| `open_references` | `scene` |
| `introductions.name` | `scene` |
| `introductions.clue` | `scene` |
| `composition_beats.index` | `verified_beat` |
| `composition_beats.kind` | `verified_beat` |
| `composition_beats.name` | `verified_beat` |
| `composition_beats.text` | `verified_beat` |
| `composition_draft` | `verified_beat` |
| `composition_omniscient` | `verified_beat` |
| `rejected_text` | `render_text` |
| `beat_kind` | `render_text` |
| `constraints.kind` | `constraint` |
| `constraints.text` | `constraint` |
| `constraints.cites` | `constraint` |
| `contradicted_facts` | `canon_record` |
| `neighborhood` | `canon_record` |

**Never receives:** `subject_true_age`, `checked_text`, `eval_text`

## Ports delivered

A port is configured like a purpose but called through its own adapter, never as a chat
completion. Its `model_config` row is its own, with no fallback to the `default` row.

### `embedding`

Embeds memory texts and retrieval queries over the OpenAI-compatible `/embeddings` surface (FR-EMBED-1). One deployment-wide model: its dimension is pinned at the first embedding, and changing the model re-embeds every hot vector in the background (FR-EMBED-3,4).

- **Hot path:** yes
- **Default model:** the `embedding` row of `model_config` only (an unset model is `model_unavailable` `not_configured`); its `params` take `timeout` only
- **Capability floor:** inputs of at least 512 tokens; vectors of 64 to 4,096 dimensions; batches of up to 64 texts
- **Returns:** one vector per text, stamped with the deployment's pinned `embedding_model_version`
- **Engine profiles:** not routable (one deployment-wide model and dimension, FR-EMBED-3)
- **Retention:** the memory texts and retrieval queries it embeds leave the deployment when this row names a hosted provider

**Receives**

| Field | Content category |
|---|---|
| `text` | `memory_text` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`

## Reserved purposes

Declared now and delivered later (docs/03 §22). Each joins the sections above, with
its floor and data-flow row, in the milestone that delivers it.

| Purpose | Kind | Milestone | Defaults to | Description |
|---|---|---|---|---|
| `belief_adjudication` | purpose | M5 | `entailment` | Adjudicated belief transitions. |
| `time_estimation` | purpose | M6 | `extraction_backstop` | Best-guess duration of a narrated skip. |
| `image_prompt` | purpose | M8 | `generation` | Writes image prompts. |
| `image_check` | purpose | M8 | — | Vision checks of image outputs; no default. |
| `image_generation` | port | M8 | — | The image-generation port (FR-IMG-1). |

## Content categories

| Category | What it is |
|---|---|
| `rating_guidance` | The story's content-rating guidance text. |
| `custom_instructions` | The story's custom instructions. |
| `character_definition` | Character names and definitions. |
| `pinned_memory` | Pinned memories of the story. |
| `rolling_summary` | The story's rolling summary segments. |
| `recent_message` | Earlier messages of the story (who, what was said and done). |
| `player_input` | The player's input for this turn. |
| `policy_text` | A policy's prompt text and opaque subject labels; for generation, only the messages of output policies an earlier draft broke. |
| `subject_true_age` | A resolved subject's true age. |
| `checked_text` | The text a guardrail check is judging (input or output). |
| `catalog_text` | A catalog entry's text under review, private fields included, with where each part sits (a rating suggestion; author-facing, FR-RATE-8). |
| `rating_description` | A content rating's name and content-policy description. |
| `scenario_text` | A scenario story's world, seeded facts, relationships, starting places and player persona, as text (#122). |
| `director_notes` | A scenario story's director notes: plot hooks, objectives, guidance, planned reveals, milestones and endings (#122). |
| `eval_text` | Text an eval case supplies or an eval's scratch story produced, compared by the quality judge; never a live story's content (#134). |
| `memory_text` | One memory record of the story: its text (an attributed line of a beat) and its kind, scored by importance scoring (#135) and embedded by the embedding port, as is a retrieval query (#138); compressed by consolidation with the canon facts of its window, each rendered as one line (#143). |
| `record_meta` | The labels of each record consolidation compresses: its index, kind, holder, visibility, position and speaker; never other text (#143). |
| `scope_meta` | The viewpoint scope, level and turns one consolidation call covers (#143). |
| `claim_text` | One claim the deterministic check left uncertain, normalized and rendered with entity names and its valid-time in story-minutes, judged by entailment (#142). |
| `canon_record` | One of the claim holder's records, rendered the same way with its holder and valid-time: only those the check fetched for the claim and the claim subject's own facts (#142). A regeneration (#151) gets the canon facts the rejected beat contradicted and their neighborhood, rendered the same way. |
| `render_text` | The beats of one render (index, kind, speaker's name, text). Salience detection (#146) reads the observable ones only (`say`, `do`, narration; never a `think` or `latent` beat or the player's input); the extraction backstop (#148) reads every beat it detects claims in, its own render's `think` and `latent` beats included, or the composed message; a regeneration (#151) reads the rejected beat it rewrites and its kind. Never another render's text. |
| `scene` | The names of the entities present in the render's scene and the last six observable beats before it, read by salience detection (#146) and the extraction backstop (#148). A structured render (#150) gets its scene (the place and who is present); the age repair gets each introduced character's name and the render's clue about it. |
| `working_memory` | A structured render's working memory (#150): the latest beats as the rendering character perceived them (its own thoughts included, never another character's or any `latent` beat), and this turn's narration and player input. |
| `retrieved_memories` | What retrieval (#141) assembled for a structured render (#150) and the rendering character may know: open threads, pins, occasions, profiles and ranked memories. |
| `canon_facts` | The canon facts retrieval found for a structured render (#150), filtered for the rendering character's viewpoint, without its own true age. |
| `constraint` | A repair's constraint on the beat it regenerates (#151): its kind (`do_not_assert`, `avoid_binding`, `policy`), its text and the ids of the records it cites. |
| `verified_beat` | A structured turn's composition (#152): its verified beats as the output viewpoint receives them (index, kind, actor's name, text; never a `think` or `latent` beat for a character viewpoint) and their deterministic assembly, which the polish may only order and join. |

## Hosted providers and retention

Each purpose sends the inputs listed in its row to the endpoint configured for it (its
`base_url`, inherited from the `default` row unless the purpose overrides it). When that
endpoint is a hosted provider, those inputs leave the deployment and are kept under that
provider's terms, outside Epistrel's erasure boundary (FR-ADMIN-8, FR-IMG-13). This includes the
true ages `guardrail_check` receives for age-conditioned policies, and the memory texts and
retrieval queries the `embedding` port sends (its own row: it inherits no endpoint). A
deployment that needs any of this data kept in-house points the purposes concerned at
self-hosted models.
`GET /admin/model-config` names the endpoint for every purpose (FR-MODEL-1).
