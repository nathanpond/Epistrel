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
| `lite_summary` | no | `default` | `none` | 8,192 tokens |

### `generation`

Writes the reply of a Lite turn from the story's prompt sections.

- **Hot path:** yes
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
- **Template:** `lite_generation.j2`
- **Capability floor:** structured output: not required; minimum context 8,192 tokens
- **Returns:** free text

**Receives**

| Field | Content category |
|---|---|
| `rating_guidance` | `rating_guidance` |
| `custom_instructions` | `custom_instructions` |
| `character_definitions.name` | `character_definition` |
| `character_definitions.definition` | `character_definition` |
| `pinned_memories` | `pinned_memory` |
| `summary_segments` | `rolling_summary` |
| `recent_messages.name` | `recent_message` |
| `recent_messages.say` | `recent_message` |
| `recent_messages.do` | `recent_message` |
| `recent_messages.text` | `recent_message` |
| `recent_messages.role` | `recent_message` |
| `voiced_characters` | `character_definition` |
| `policy_constraints` | `policy_text` |
| `player_input.name` | `player_input` |
| `player_input.say` | `player_input` |
| `player_input.do` | `player_input` |

**Never receives:** `subject_true_age`, `checked_text`

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

**Never receives:** `rating_guidance`, `custom_instructions`, `character_definition`, `pinned_memory`, `rolling_summary`, `recent_message`, `player_input`

### `lite_summary`

Folds old messages into a Lite story's rolling summary (FR-LITE-2).

- **Hot path:** no
- **Default model:** the `default` row of `model_config`, unless the purpose has its own
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

**Never receives:** `policy_text`, `subject_true_age`, `checked_text`

## Reserved purposes

Declared now and delivered later (docs/03 §22). Each joins the sections above, with
its floor and data-flow row, in the milestone that delivers it.

| Purpose | Kind | Milestone | Defaults to | Description |
|---|---|---|---|---|
| `regeneration` | purpose | M4 | — | Regenerates a span under corrected constraints. |
| `entailment` | purpose | M4 | — | The fuzzy residual of verify. |
| `consolidation` | purpose | M4 | — | Summaries and reflections (worker). |
| `importance_scoring` | purpose | M4 | — | Scores one memory's importance. |
| `salience_detection` | purpose | M4 | — | Detects signals that need commitment. |
| `extraction_backstop` | purpose | M4 | — | Model layer of claim detection. |
| `quality_judge` | purpose | M4 | — | Pairwise prose comparisons (eval only). |
| `belief_adjudication` | purpose | M5 | `entailment` | Adjudicated belief transitions. |
| `time_estimation` | purpose | M6 | `extraction_backstop` | Best-guess duration of a narrated skip. |
| `rating_suggestion` | purpose | M3 | `guardrail_check` | Per-rating content-policy review. |
| `image_prompt` | purpose | M8 | `generation` | Writes image prompts. |
| `image_check` | purpose | M8 | — | Vision checks of image outputs; no default. |
| `embedding` | port | M4 | — | The embedding port (FR-EMBED). |
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

## Hosted providers and retention

Each purpose sends the inputs listed in its row to the endpoint configured for it (its
`base_url`, inherited from the `default` row unless the purpose overrides it). When that
endpoint is a hosted provider, those inputs leave the deployment and are kept under that
provider's terms, outside Epistrel's erasure boundary (FR-ADMIN-8, FR-IMG-13). This includes the
true ages `guardrail_check` receives for age-conditioned policies. A deployment that needs any
of this data kept in-house points the purposes concerned at self-hosted models.
`GET /admin/model-config` names the endpoint for every purpose (FR-MODEL-1).
