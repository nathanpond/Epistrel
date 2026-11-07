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
| `belief_adjudication` | yes | `entailment` | `parse_validate_retry` | 8,192 tokens |
| `salience_detection` | yes | `default` | `parse_validate_retry` | 8,192 tokens |
| `extraction_backstop` | yes | `default` | `parse_validate_retry` | 8,192 tokens |
| `regeneration` | yes | `generation` | `parse_validate_retry` | 8,192 tokens |
| `time_estimation` | yes | `extraction_backstop` | `parse_validate_retry` | 8,192 tokens |
| `rewrite_parse` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `rewrite_record` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `directive_parse` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `lite_rewrite_reach` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `lite_rewrite_parse` | no | `default` | `parse_validate_retry` | 8,192 tokens |
| `image_prompt` | no | `generation` | `parse_validate_retry` | 8,192 tokens |
| `image_check` | no | none (own row only) | `parse_validate_retry` | 8,192 tokens |

### `generation`

Writes the reply of a Lite turn from the story's prompt sections; in a structured story (#150) the typed beats of one character's or the narrator's render (a `response_schema`: constrained decoding or parse-validate-retry), the age repair, the composition of the turn's verified beats into its message (#152), the director's ordering of a sequential turn's primaries (#174) and its judgment of a simultaneous turn's conflicting actions (#175).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `lite_generation.j2`; variants: structured `structured_generation.j2`, narrator `narrator_generation.j2`, age_repair `age_repair.j2`, composition `composition.j2`, director_order `director_order.j2`, director_tiebreak `director_tiebreak.j2`
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
| `own_age` | `character_definition` |
| `beliefs` | `belief_state` |
| `time_belief` | `belief_state` |
| `clock_now` | `scene` |
| `private_memories` | `working_memory` |
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
| `length_target` | `constraint` |
| `belief_steps` | `constraint` |
| `clock_contract` | `constraint` |
| `player_input.name` | `player_input` |
| `player_input.say` | `player_input` |
| `player_input.do` | `player_input` |
| `player_input.heard` | `player_input` |
| `player_authority` | `constraint` |
| `player_requests` | `player_input` |
| `tracked_values` | `tracked_values` |
| `lite_time` | `scene` |
| `tracked_syntax` | `tracked_values` |
| `external_data` | `external_data` |
| `pending_outcomes` | `tracked_values` |
| `scene` | `scene` |
| `working_memory` | `working_memory` |
| `just_now` | `working_memory` |
| `others_present` | `scene` |
| `interjections` | `constraint` |
| `retrieved_memories` | `retrieved_memories` |
| `canon_facts` | `canon_facts` |
| `open_references` | `scene` |
| `director_candidates.id` | `character_definition` |
| `director_candidates.name` | `character_definition` |
| `director_candidates.card` | `character_definition` |
| `tiebreak_conflicts.object` | `verified_beat` |
| `tiebreak_conflicts.name` | `verified_beat` |
| `tiebreak_conflicts.actions.character_id` | `verified_beat` |
| `tiebreak_conflicts.actions.name` | `verified_beat` |
| `tiebreak_conflicts.actions.kind` | `verified_beat` |
| `tiebreak_conflicts.actions.text` | `verified_beat` |
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
- **Template:** `guardrail_check.j2`; variants: figures `guardrail_figures.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `GuardrailVerdict` (`violation`, `confidence`), validated strictly; no free-text field
- **Hosted providers:** When this purpose is routed to a hosted provider, the images and true ages it receives leave the deployment, are kept under that provider's terms and are outside Epistrel's erasure boundary; to keep them in-house, route both `guardrail_check` and `image_check` to self-hosted models.

**Receives**

| Field | Content category |
|---|---|
| `text` | `checked_text` |
| `policy_prompt` | `policy_text` |
| `subjects.label` | `policy_text` |
| `subjects.age` | `subject_true_age` |
| `subjects.present` | `subject_true_age` |
| `external_data` | `external_data` |
| `image_subjects.label` | `subject_label` |
| `image_subjects.true_age` | `subject_true_age` |
| `image_subjects.classification` | `subject_true_age` |
| `image_subjects.visual_descriptors` | `visual_descriptor` |
| `image_subjects.mentioned_as` | `checked_text` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `image`, `image_prompt`, `caption_alt_text`, `reference_image`

**Never receives (docs/02 §6):** other story text; beliefs; names an image prompt lacks

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

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

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
| `subjects.present` | `subject_true_age` |
| `elements.kind` | `catalog_text` |
| `elements.location` | `catalog_text` |
| `elements.text` | `catalog_text` |

**Never receives:** `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `checked_text`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

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

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

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

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

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

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `belief_adjudication`

Decides what one salient witness makes of a say it heard: accept, reject, suspend or only note that it was said, from its own records, its trust in the speaker and the intent it reads; optional work within the turn budget, defaulted by the rules otherwise (FR-BELIEF-3).

- **Hot path:** yes
- **Default model:** the `entailment` row of `model_config`, then the `default` row, per field, unless the purpose has its own
- **Template:** `belief_adjudication.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `BeliefAdjudicationResult` (`decision`, `confidence`, `reasons`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `witness` | `belief_record` |
| `witness_beliefs.statement` | `belief_record` |
| `witness_beliefs.source` | `belief_record` |
| `witness_beliefs.confidence` | `belief_record` |
| `trust` | `belief_record` |
| `priors.outcomes` | `belief_record` |
| `priors.existing_confidence` | `belief_record` |
| `priors.rule_default` | `belief_record` |
| `utterance.speaker` | `utterance_text` |
| `utterance.text` | `utterance_text` |
| `utterance.manner` | `utterance_text` |
| `utterance.claims` | `utterance_text` |
| `perceived_intent` | `utterance_text` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_state`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `salience_detection`

Tells content placed to be noticed from background texture in one render's beats, naming the proposition each salient span raises (FR-COMMIT-3).

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `salience.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `SalienceResult` (`salient`, `texture`, `characters`, `confidence`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `beats.index` | `render_text` |
| `beats.kind` | `render_text` |
| `beats.speaker` | `render_text` |
| `beats.text` | `render_text` |
| `scene_context.present_entities` | `scene` |
| `scene_context.last_beats` | `scene` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

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
| `external_data` | `external_data` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

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
| `own_age` | `character_definition` |
| `beliefs` | `belief_state` |
| `time_belief` | `belief_state` |
| `clock_now` | `scene` |
| `private_memories` | `working_memory` |
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
| `length_target` | `constraint` |
| `belief_steps` | `constraint` |
| `clock_contract` | `constraint` |
| `player_input.name` | `player_input` |
| `player_input.say` | `player_input` |
| `player_input.do` | `player_input` |
| `player_input.heard` | `player_input` |
| `player_authority` | `constraint` |
| `player_requests` | `player_input` |
| `tracked_values` | `tracked_values` |
| `lite_time` | `scene` |
| `tracked_syntax` | `tracked_values` |
| `external_data` | `external_data` |
| `pending_outcomes` | `tracked_values` |
| `scene` | `scene` |
| `working_memory` | `working_memory` |
| `just_now` | `working_memory` |
| `others_present` | `scene` |
| `interjections` | `constraint` |
| `retrieved_memories` | `retrieved_memories` |
| `canon_facts` | `canon_facts` |
| `open_references` | `scene` |
| `director_candidates.id` | `character_definition` |
| `director_candidates.name` | `character_definition` |
| `director_candidates.card` | `character_definition` |
| `tiebreak_conflicts.object` | `verified_beat` |
| `tiebreak_conflicts.name` | `verified_beat` |
| `tiebreak_conflicts.actions.character_id` | `verified_beat` |
| `tiebreak_conflicts.actions.name` | `verified_beat` |
| `tiebreak_conflicts.actions.kind` | `verified_beat` |
| `tiebreak_conflicts.actions.text` | `verified_beat` |
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

### `time_estimation`

Proposes a best-guess duration, with its reason and a confidence, for a narrated skip a beat implies but does not declare, from what the scene established; optional work within the turn budget (FR-CLOCK-7).

- **Hot path:** yes
- **Default model:** the `extraction_backstop` row of `model_config`, then the `default` row, per field, unless the purpose has its own
- **Template:** `time_estimation.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `TimeEstimate` (`reason`, `ends_at`, `minutes`, `confidence`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `narrated_text` | `render_text` |
| `activity_context` | `scene` |
| `established_times` | `scene` |
| `clock` | `scene` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `rewrite_parse`

Parses an author's history-rewrite instruction into structured proposition, record and State changes, and names what it cannot decide, on the author's side-channel, never in a turn (FR-REWRITE-1,2).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `rewrite_parse.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `RewriteParseResult` (`changes`, `ambiguities`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `instruction` | `author_instruction` |
| `answers` | `author_instruction` |
| `entities.name` | `author_instruction` |
| `entities.kind` | `author_instruction` |
| `entities.aliases` | `author_instruction` |
| `predicates` | `author_instruction` |
| `states.name` | `author_instruction` |
| `states.type` | `author_instruction` |
| `states.scope` | `author_instruction` |
| `states.unit` | `author_instruction` |
| `states.aliases` | `author_instruction` |
| `states.owner` | `author_instruction` |
| `clock` | `author_instruction` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `rewrite_record`

Repairs one record a history rewrite contradicts: the least change that makes its text agree with the new canon, keeping its speaker, voice and intent, on the author's side-channel, never in a turn (FR-REWRITE-3).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `rewrite_record.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `RewrittenRecord` (`text`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `record_text` | `checked_text` |
| `record_kind` | `author_instruction` |
| `old_value` | `author_instruction` |
| `new_canon` | `author_instruction` |
| `guidance` | `author_instruction` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `directive_parse`

Classifies an author's directive as guidance for the next turn or a change to the world, now or in the past, and parses its changes, on the author's side-channel, never in a turn (FR-DIRECT-1,2).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `directive_parse.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `DirectiveParseResult` (`classification`, `temporal_scope`, `changes`, `steer_text`, `binding`, `standing`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `instruction` | `author_instruction` |
| `answers` | `author_instruction` |
| `entities.name` | `author_instruction` |
| `entities.kind` | `author_instruction` |
| `entities.aliases` | `author_instruction` |
| `predicates` | `author_instruction` |
| `states.name` | `author_instruction` |
| `states.type` | `author_instruction` |
| `states.scope` | `author_instruction` |
| `states.unit` | `author_instruction` |
| `states.aliases` | `author_instruction` |
| `states.owner` | `author_instruction` |
| `clock` | `author_instruction` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `lite_rewrite_reach`

Finds the lines of a Lite story's transcript a correction to its past contradicts or mentions and proposes the least fix of each, on the author's side-channel, never in a turn (FR-LITE-8): the Lite reach is model-searched, not structurally complete.

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `lite_rewrite_reach.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `LiteRewriteReachResult` (`hits`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `instruction` | `author_instruction` |
| `lines.turn` | `recent_message` |
| `lines.speaker` | `recent_message` |
| `lines.text` | `recent_message` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `lite_rewrite_parse`

Reads a Lite story's history-rewrite instruction as one change statement, says whether it changes the world, and asks what it cannot decide, on the author's side-channel, never in a turn (FR-LITE-8).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `lite_rewrite_parse.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `LiteRewriteParseResult` (`change_statement`, `world_change`, `ambiguities`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `instruction` | `author_instruction` |
| `answers` | `author_instruction` |
| `recent.turn` | `recent_message` |
| `recent.speaker` | `recent_message` |
| `recent.text` | `recent_message` |
| `author_facts` | `author_instruction` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `image_prompt`

Writes an image prompt from what the image's audience has perceived: the subjects' visual identities and apparent age bands, the scene, the visual style and the rating's image guidance (FR-IMG-2). Catalog candidates (#259) and story images (#263).

- **Hot path:** no
- **Default model:** the `generation` row of `model_config`, then the `default` row, per field, unless the purpose has its own
- **Template:** `image_prompt.j2`; variants: base_appearance `image_prompt_base_appearance.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens
- **Returns:** `ImagePromptResult` (`prompt`), validated strictly; its free text is author-facing

**Receives**

| Field | Content category |
|---|---|
| `subjects.description` | `visual_descriptor` |
| `subjects.age_band` | `visual_descriptor` |
| `subjects.base_appearance_prompt` | `visual_descriptor` |
| `scene` | `scene` |
| `visual_style` | `visual_descriptor` |
| `rating_image_guidance` | `rating_guidance` |
| `purpose` | `constraint` |

**Never receives:** `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `working_memory`, `retrieved_memories`, `canon_facts`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `image_prompt`, `caption_alt_text`, `reference_image`

**Never receives (docs/02 §6):** true ages; restricted references; anything the image's audience has not perceived

### `image_check`

Checks one image against the image-output policies, with its prompt, caption, alt text and the safety context's subject labels, true ages and visual descriptors (FR-IMG-3,13). A vision-capable model; no default: unset, every image-output policy evaluation fails closed (`image_check_unconfigured`).

- **Hot path:** no
- **Default model:** none: its own `image_check` row of `model_config` only, never the `default` row (unset until configured)
- **Template:** `image_check.j2`
- **Capability floor:** structured output: constrained `json_schema` decoding, or output reliable enough to parse, validate and retry; minimum context 8,192 tokens; image input (vision)
- **Returns:** `ImageCheckVerdict` (`verdicts`), validated strictly; no free-text field
- **Hosted providers:** When this purpose is routed to a hosted provider, the images and true ages it receives leave the deployment, are kept under that provider's terms and are outside Epistrel's erasure boundary; to keep them in-house, route both `guardrail_check` and `image_check` to self-hosted models.

**Receives**

| Field | Content category |
|---|---|
| `image` | `image` |
| `prompt` | `image_prompt` |
| `caption` | `caption_alt_text` |
| `alt_text` | `caption_alt_text` |
| `subjects.label` | `subject_label` |
| `subjects.true_age` | `subject_true_age` |
| `subjects.classification` | `subject_true_age` |
| `subjects.visual_descriptors` | `visual_descriptor` |
| `policies.policy_id` | `policy_text` |
| `policies.text` | `policy_text` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `reference_image`

**Never receives (docs/02 §6):** story text; beliefs; names the prompt lacks

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

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `image_prompt`, `caption_alt_text`, `reference_image`

### `image_generation`

The image adapter (FR-IMG-1): generates images through the `ImageGenerator` port (`orchestrator.images.port`) with the adapter its row names (`params.adapter`; OpenRouter is bundled, #255). With no adapter, model or key, every generation entry point answers 409 `not_configured` and uploads keep working.

- **Hot path:** no
- **Default model:** the `image_generation` row of `model_config` only, never the `default` row; its `params` take `adapter` (a registered adapter, or `null`: off), `default_size`, `default_aspect_ratio`, `default_quality`, `image_capability_ttl_s` and `request_timeout_s`. With no adapter, model or key, every generation entry point answers 409 `not_configured`, and uploads keep working
- **Capability floor:** generates images from a text prompt; seeds, reference images, sizes and aspect ratios are discovered per endpoint, never assumed (FR-IMG-1)
- **Returns:** the generated images' bytes and media types, with usage and cost when the provider reports them (an unpriced call is `unknown`, never 0)
- **Engine profiles:** not routable (one deployment-wide adapter)
- **Retention:** the built prompts, their parameters and the reference images it is given go to the image provider this row names; it never receives true ages or the safety context

**Receives**

| Field | Content category |
|---|---|
| `prompt` | `image_prompt` |
| `negative_prompt` | `image_prompt` |
| `seed` | `image_prompt` |
| `size` | `image_prompt` |
| `aspect_ratio` | `image_prompt` |
| `quality` | `image_prompt` |
| `n` | `image_prompt` |
| `reference_images` | `reference_image` |

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`, `policy_text`, `subject_true_age`, `checked_text`, `catalog_text`, `rating_description`, `scenario_text`, `director_notes`, `eval_text`, `memory_text`, `record_meta`, `scope_meta`, `claim_text`, `canon_record`, `render_text`, `scene`, `working_memory`, `retrieved_memories`, `canon_facts`, `constraint`, `verified_beat`, `belief_record`, `belief_state`, `utterance_text`, `author_instruction`, `tracked_values`, `external_data`, `image`, `subject_label`, `visual_descriptor`, `caption_alt_text`

**Never receives (docs/02 §6):** true ages; the safety context

## Reserved purposes

Declared now and delivered later (docs/03 §22). Each joins the sections above, with
its floor and data-flow row, in the milestone that delivers it.

None: every purpose and port FR-MODEL-2 names is delivered.

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
| `belief_record` | One salient witness's view for testimony adjudication (#162): its name, the records the deterministic consistency check read for the claim (its own beliefs and the canon it may read, rendered like claims), its trust in the speaker, its earlier outcomes with that speaker and the rules' default decision. Never another character's private record or canon the witness cannot know. |
| `belief_state` | A structured character render's own belief state (#170, FR-TURN-8): its beliefs as "you suspect / believe / know ..." lines, its nested beliefs ("you think Bobby believes ...") and the speech acts it heard ("you heard Bobby say ..."). Its own partition only; never another character's, a scalar, a provenance or a divergence. |
| `utterance_text` | The say a witness heard (#162): the speaker's name, the words as the witness perceived them, the visible manner, the claims they carry and the intent the witness reads. |
| `author_instruction` | An author's history-rewrite instruction and free-text answers (#223, FR-REWRITE-1,2), and the story's names it is normalized against: its entities, the registered predicates, its States and the clock. Never transcript text, a memory or a belief. |
| `tracked_values` | A structured render's tracked values (#187, FR-TURN-11): for a character's render, the player-visible States it may change (name, type, scope, unit, aliases, allowed values) and the effect syntax, never a value; for the narrator's, every State with its current value and constraints. |
| `external_data` | A value an external-data provider answered (#208, FR-EXT-5, NFR-SEC-2): untrusted content, typed and cleaned, rendered only inside a fenced, labelled data block (<external name=... tier=...>) and never as instructions; the answer's JSON keys never. The narrator's render takes it; a character learns a value only as a belief. |
| `image` | An image's own pixels (#254): a generated output, or an upload when a checker is configured, checked by `image_check` against the image-output policies. |
| `subject_label` | An opaque label of a subject in an image's safety context (`character_1` ...): never a name the prompt does not already contain (FR-IMG-13). |
| `visual_descriptor` | What an image shows, in visual terms only: a subject's perceivable appearance and apparent age band, the visual style, and in a safety context the descriptors the prompt already uses for a subject. Never a true age, a name the prompt lacks or story text. |
| `image_prompt` | A built image prompt and its negative prompt (#254): what the image adapter generates from (with its seed, size or aspect ratio, quality and count) and what `guardrail_check` and `image_check` judge. |
| `caption_alt_text` | An image's caption and alt text, judged with the image. |
| `reference_image` | A reference image the image adapter is given: one the image's audience has perceived (FR-IMG-5), sent as a signed URL or a data URL (#255). |

## Hosted providers and retention

Each purpose sends the inputs listed in its row to the endpoint configured for it (its
`base_url`, inherited from the `default` row unless the purpose overrides it). When that
endpoint is a hosted provider, those inputs leave the deployment and are kept under that
provider's terms, outside Epistrel's erasure boundary (FR-ADMIN-8, FR-IMG-13). This includes the
true ages `guardrail_check` receives for age-conditioned policies, the images and true ages
`image_check` receives (its own row: it has no default), the memory texts and
retrieval queries the `embedding` port sends, and the prompts and reference images the
`image_generation` port sends to its image provider (each port's own row: it inherits no
endpoint). A deployment that needs any of this data kept in-house points the purposes
concerned at self-hosted models; for the safety path, both `guardrail_check` and `image_check`.
`GET /admin/model-config` names the endpoint for every purpose and port (FR-MODEL-1): its
`endpoint` is the host of the effective `base_url`.
