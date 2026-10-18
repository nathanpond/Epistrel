# Epistrel — Operations

Backup, restore and the erasure ledger for a deployment (FR-ADMIN-4, FR-ADMIN-7, FR-ADMIN-8; docs/02 §12). Per-story backup and portability use story bundles (FR-STORE-7); this page is about the whole deployment.

## Backups

A deployment's state lives in three places, and a backup takes all three, separately:

1. **The database.** `epistrel backup dump` writes `pg_dump -Fc` of the database `EPISTREL_DATABASE_URL` names to `./epistrel-<UTC timestamp>.dump` (`--output` to choose; `--pg-dump` names the executable). It needs PostgreSQL's client tools (`pg_dump` 16). It is the same as running `pg_dump --format=custom` yourself. The dump carries the `erasure_ledger` table: a copy of the erasure ledger as it was when the dump was taken.
2. **The media.** The `media` volume (`EPISTREL_MEDIA_ROOT`, `/var/lib/epistrel/media` in the image), or the S3 bucket (`EPISTREL_MEDIA_STORE=s3`).
3. **The erasure ledger.** The `erasure-ledger` volume (`EPISTREL_ERASURE_LEDGER_DIR`, `/var/lib/epistrel/erasure-ledger` in the image): the file `ledger.jsonl`. Keep it on storage that outlives your database backups, and copy it somewhere safe as well. `epistrel backup dump` deliberately does not bundle it: the ledger governs the backups, so it must never be restored *from* one.

## Restore

1. Stop Epistrel (`docker compose stop engine`, or every `serve` and `worker` process).
2. Restore the database: `pg_restore --clean --if-exists --dbname=<database> <dump>`, then run the migrations if the dump is from an older version (the image's entrypoint does this at start).
3. Restore the media volume or bucket from the same point in time.
4. **Leave the erasure ledger as it is.** Do not restore `ledger.jsonl` from the backup. The live file is what makes the restore safe.
5. Start Epistrel. Before it serves a request, it compares the ledger file with the restored database. Every erasure the file holds and the database lacks becomes **pending**, and the story it was made in is `blocked_by_erasure`. Every read and write of a blocked story is 503 `erasure_pending` (with `Retry-After: 30`), and so is `POST /admin/erasure` naming it, dry run or not. While a catalog erasure is pending, everything its reapply will touch is held the same way: a call naming the erased catalog character or persona, its companion scenario, a catalog character, scenario, persona, media asset or upload that names or depicts the subject, a stored result of `GET /operations/{id}` (or an `Idempotency-Key` replay) that names it, a story created from any of them, or another catalog-scope erasure, dry run or not, is 503 `erasure_pending` (reads and writes alike, deletes included). Catalog lists and searches, `GET /personas` and `GET /media` leave those entries out, and `GET /principals/{id}` lists them with a `null` name. An undelivered feedback export's content is 503 `erasure_pending` too, and the export stays undelivered until the reapply has redacted it. `GET /stories` still lists it with its status, and `DELETE /stories/{id}` still deletes it. Epistrel then reapplies each pending erasure itself, catalog erasures first, in the background (10 minutes at start-up, then every 5 minutes until done), and unblocks each story when nothing of it is pending. `GET /admin/erasure/status` shows what is pending. `POST /admin/erasure/reconcile` runs the reconciler at once. `/health` reports `checks.erasure_pending` and is `degraded` while it is above zero.

**A restore plus the ledger's reapplication reproduces the post-erasure state, never the pre-erasure one.** Restoring a backup taken before an erasure does not bring the erased person back: no restored story is served until the ledger has been reapplied to it, and afterwards its texts read `[erased]` where they named the subject, with an erasure marker where each deleted record was. The history as it was before the erasure cannot be rebuilt from a backup.

A reapply that fails (for example, `EPISTREL_CONFIG_KEY` is unset, so the erasure's keyed hashes cannot be made) leaves its story blocked. The error class and the attempts are in `GET /admin/erasure/status`, and the 5-minute retry keeps trying.

## Erasure ledger

Every erasure (`POST /admin/erasure`) appends one line to `ledger.jsonl` before it commits, and fsyncs it. The line holds identifiers, counts and keyed hashes, never a name: `{erasure_id, subject: {kind, id}, scope, story_id, at, principal, operation_id, counts, scan_term_hashes, ledger_hash_prev, ledger_hash}`. The same row, with the same hashes, goes into the `erasure_ledger` table, so every database backup carries a copy. If the file cannot be written, the erasure is refused (503 `ledger_unavailable`) and nothing is erased.

The lines form a hash chain. `ledger_hash` is the SHA-256 of the line's canonical JSON (sorted keys, compact separators) without that field, and `ledger_hash_prev` is the previous line's hash (64 zeros for the first line). A changed, removed or reordered line breaks the chain. **The file is the authority:**

- A line the database lacks becomes pending, and is reapplied as described above.
- A database row the file lacks (for example, a new or lost ledger volume) is appended to the file, chained after its last line.
- A row whose fields differ between the two refuses the start (exit 78 from `epistrel serve`), since one of them was tampered with.
- A broken chain, a malformed line, or a directory that cannot be written refuses the start too (exit 78), with the reason on standard error.

The file is append-only and small: one line per erasure, so it needs no rotation at hobby scale.

### The resurrection guard

The keyed hashes also stop an erasure being undone by work that was already in flight (FR-ADMIN-9). Every write to an erased story is checked before it commits: a turn reply, a background job's result, a model output, and a client's write whose context predates the erasure. A write that names the erased subject, or references one of the deleted characters, is refused with 409 `erased_subject_referenced` and nothing is written. Text a client writes after the erasure is not refused, even if it types the name. A fork of the story copies its ledger rows, so the fork is guarded too.

**Rotating `EPISTREL_CONFIG_KEY` disables the guard for earlier erasures.** Their hashes were made with the old key, and the names are gone by design, so they cannot be hashed again. The erasures themselves stand: nothing is un-erased. Only text written afterwards that names the subject is no longer refused.

A name word that is also a common word (`Will`, `Rose`) is guarded only capitalised in the middle of a sentence, so a later reply saying "it will rain" is not refused (ADR-154).

### Candidates and the subject index

An erasure removes the mentions that certainly refer to the subject: its names and aliases, and the words of its names. A name word that is also a common word, capitalised at the start of a sentence ("Will you come?"), is a **candidate**. A dry run (`dry_run: true`) lists every candidate with its `candidate_id`, where it is and a snippet of the text around it, and the erasure removes only the candidates its `confirm_candidates` names. The snippet is returned by the dry run and never stored. `candidates_remaining` in the response, and in the ledger's counts, says how many were left in place. The ledger keeps the confirmed ids, never text, so a reapply after a restore removes them too.

The subject-reference index records where each mention is (field, offsets and rule), never its text. **After upgrading to schema 0044, run the one-time re-index once:** `python -m epistrel.tools.reindex_subjects --write`. It rebuilds the rows written before mentions had positions. An erasure re-indexes its own story first in any case.

**After upgrading past v0.1.1, run the catalog re-index once:** `python -m epistrel.tools.reindex_subjects --catalog --write`. Scenario drafts and versions saved before then were not indexed for the strings inside their starting-state objects, their roles' starting values or their own policies (#323, #422), so a catalog-scope erasure would not find a name written only there until they are re-indexed. Without `--write` the tool lists the rows it would add. A catalog-scope erasure does not re-index the catalog first; its `residue` count still reports any mention left behind.

## Verify

`epistrel backup verify <dump>` checks a dump without restoring it. It reads the dump's `erasure_ledger` and `events` data through `pg_restore` (`--pg-restore` names the executable) and reports:

- **Expected gaps.** Each erasure marker (the erased record's table and id, its story and scope, never content) and the number of events redacted in place. These are erasures, not corruption: they never fail the check.
- **The dump's ledger rows against the live file** (`--ledger <dir>`, else `EPISTREL_ERASURE_LEDGER_DIR`). The erasures newer than the dump are listed, because a restore of this dump reapplies them.

Output is text, or JSON with `--json`. Exit codes:

| Exit | Meaning |
|---|---|
| 0 | The dump is consistent with the ledger. Gaps are informational. A dump taken before the ledger table existed is noted and passes. |
| 2 | The ledger file's chain is broken, or the dump holds ledger rows the file lacks or disagrees with. The file is not this deployment's, or it was altered. |
| 3 | The dump cannot be read. |
| 4 | `pg_restore` was not found. |
| 5 | There is no ledger file to check against. |

Audit views show the same markers: `GET /stories/{id}/history?viewpoint=omniscient&include_audit=1` lists an `erasure_marker` item where each erased record was.

<!-- erasure-boundary:start -->
## Erasure boundary

**In scope.** An erasure removes or redacts the subject in:

- events and beats (redacted in place; each deleted record leaves an erasure marker)
- projections: transcript, story texts and heads, rebuilt from the redacted events
- rolling-summary segments (summarised again from the redacted window, or left redacted)
- pinned memories and custom instructions
- stored idempotency results
- catalog drafts and published versions, and scenario reports
- personas
- media metadata and bytes (declared subjects, captions, alt text and prompts)
- media assets that depict or name only the subject: under `erasure_shared_media = purge` (the default) the asset and its bytes are deleted; under `retire`, an asset another record still uses stays on disk, retired, with the subject removed from its metadata
- message feedback and its history
- feedback exports not yet downloaded (the subject is redacted in their cases)
- story snapshots (scenario, characters, persona) and deferred genesis items
- the story's creation report: the subject's deferred genesis items, and her cast card (see below for what a story-scope erasure keeps of a catalog character's card)
- the subject-reference index
- a backup restored after the erasure: its stories are not served until the erasure ledger, kept outside the database and copied into every backup, has been reapplied

**Out of scope.** An erasure cannot reach:

- exports already delivered
- data retained by external model/image/data providers (including hosted guardrail or image check providers)
- subjects appearing in images only visually with no declared or named reference
- backups taken before the erasure, until restored and reconciled
- logs, which never carry story content
- mentions the matcher classes as ambiguous (a name word that is also a common word, capitalised at the start of a sentence) unless the operator confirms them, and nicknames never declared as aliases; an erasure reports how many candidates remain (`candidates_remaining`). A word of the subject's name on its own is not matched when another character in the same scope also uses it (a shared surname) or when it is a title or particle (`Dr`, `de`); a common word (`will`, `rose`) is matched only capitalised. Names match in any case, fully case-folded (`Strauß` is `STRAUSS`, `Yılmaz` is `YILMAZ`), a hyphenated name by its halves at any dash, and a soft hyphen inside a word is ignored (ADR-154, ADR-155, ADR-156)
- a cast catalog character's public card in the creation report of a story she was erased from at story scope: its labels and public description stay, with its structural fields (its kind, ids, source, version and status values), while its name is redacted and its images and content rating are emptied, because the card is public catalog data, until a catalog-scope erasure of that catalog character empties it too (docs/04 C-15)

**Logs.** Operational logs contain no story content by default (NFR-SEC-4).
<!-- erasure-boundary:end -->

The same statement is served by `GET /admin/erasure/boundary` as data, and it ends the API description (`/openapi.json`, `info.description`). A test keeps the three equal.

## Feedback exports

`POST /admin/feedback/export` (FR-FB-4) writes rated messages as labelled cases, one JSON Lines file per export, only while the deployment setting `feedback_export` is on (it is off by default; `PUT /admin/settings {"feedback_export": true}`). The file is kept in the blob store at `exports/<export id>.jsonl` (the media root, or the S3 bucket) for seven days, then the media sweep deletes it; `DELETE /admin/feedback/exports/{id}` deletes it sooner. It is served by `GET /admin/feedback/exports/{id}/content` behind the service credential only. Every export stays recorded in `feedback_export_log` (who, when, the window and filters, the case count), also after its file and its stories are deleted; `GET /admin/feedback/exports` lists them. A case holds the message, the player input, its position, the configuration it was produced under, and the rating with its tags and text: never a principal, a credential or a setting.

<!-- feedback-export-boundary:start -->
Delivered exports are outside the erasure boundary (FR-ADMIN-8: exports already delivered): an export's file counts as delivered once it has been downloaded, and an erasure cannot reach a copy outside the deployment. Until its first download, an erasure redacts the subject in the file's cases.
<!-- feedback-export-boundary:end -->

## Retrieval index tuning

The embedding vectors' HNSW index (`ix_memories_embedding_hnsw`, cosine) is built when the first embedding pins the dimension, and again when a model change retypes the column. It uses the deployment settings `embedding_hnsw_m` (default 16) and `embedding_hnsw_ef_construction` (default 64). A change to either setting applies to the next build only. To rebuild an existing index with the new values, run `REINDEX INDEX CONCURRENTLY ix_memories_embedding_hnsw;` against the database after the `PUT /admin/settings`. Searches keep running during the rebuild.

`embedding_hnsw_ef_search` (default 40) is the candidate list size of each retrieval's vector search. Every retrieval sets it on its own transaction (`SET LOCAL hnsw.ef_search`) together with pgvector's iterative scan (`hnsw.iterative_scan = relaxed_order`), so filtered searches still fill their top-k. A change applies to the next retrieval and needs no rebuild.

`retrieval_vector_k`, `retrieval_lexical_k` (50 each), `retrieval_rrf_k` (60) and `retrieval_rehydrate_max_rows` (20) size and fuse the search arms. The ranking weights, the recency half-life and the token budget are engine-profile dials (`memory.episodic`).

## Verification escalation

A structured story checks each claim deterministically first. Only a claim the engine cannot decide is sent to the `entailment` model, and only when the story's profile has `verification.level = entailment`. Three engine-profile dials control this:

- `verification.entailment_gate` (default 1.0) sends every undecided claim to the model. The gate is tightened per predicate shape, never by lowering this default.
- `verification.entailment_min_confidence` (default 0.6) is the least confidence a model answer needs.
- `verification.safe_disposition` (`repair`, the default, or `surface`) sets what happens to a claim nobody could decide. With `repair`, the sentence is rewritten. With `surface`, it stands and the claim is listed for the author.

A model answer that is unparseable, below the floor, or `novel` never counts as `supported`, and neither does an unreachable model: each takes the safe disposition.

Two deployment settings bound what one verification reads. `neighborhood_max_facts` (default 50) caps the facts, closed sets, projections and memories of a repair's dependency cluster. `entailment_max_records` (default 30) caps the records one entailment request carries.

## Claim detection

A structured story looks for every claim a render makes before it checks any (FR-VERIFY-13,14). Each render, and the final composed message, passes through the declared structure (what its beats declared), deterministic detectors (English word tables for possession, transfer, consumption, injury and movement, quantities, time expressions, uncovered mentions, and the names and values hidden from the speaker), and, at `verification.level` `extraction` or `entailment`, the `extraction_backstop` model purpose. `verification.extraction_mode` decides when the model runs: `gated` (the default) only on the beats a detector flagged, or that declared nothing; `always` on every render (the `Structured (M4)` profile sets it). When the model is unreachable or its answer stays invalid, the render keeps the deterministic layers and the turn reports `extraction_degraded`.

Under `gated`, a share of renders (`extraction_shadow_sample_rate`, deployment setting, default 0.1) is a shadow sample: after the turn commits, the `extraction_shadow` job runs the extraction over the beats gating skipped, under its own budget, and records the claims it would have missed. `GET /admin/evals/extraction-shadow` sums them over a window (`from`, `to`, default the last 30 days; optional `story_id`): samples, missed claims by category, the miss rate, and, apart, how many claims were detected and how many were checked. `protected_index_max` (default 5,000) caps the hidden names and values one turn matches; past it the turn report says `protected_index_truncated`. The counters `epistrel_claims_detected_total{layer,category}` and `epistrel_claims_checked_total{verdict,category}` count detection and checking as separate series: every detected claim is checked, while how many claims are detected at all is the measured rate.

## Repair

A structured story whose profile has the `repair` module on (`repair.max_attempts` of 1 or more; `Structured (M4)` sets 1, the default is 0) repairs every rendered beat whose claims are contradicted, undecided under the `repair` safe disposition, or unpermitted novelty (#151). The ladder tries, in order: dropping the sentences that carry the claims, swapping a functional claim's object for canon's, regenerating the beat through the `regeneration` purpose (which shares the generation model unless it has its own `model_config` row), and the stored safe fallback. Every correction passes the output policies and has its claims detected and checked again before it is accepted. A regeneration starts only when the turn's budget can reserve its render, its extraction and one re-check per model-assisted output policy; otherwise the turn reports `repair_deterministic`, and a fallback reports `safe_fallback`. The rejected texts are `render_rejected` audit events (with the beat's `verify_verdict`), kept for the audit retention like every audit record; `turn_report.repairs` lists each repair's rung, class and neighborhood. `cascade_threshold` (deployment setting, default 5) is the neighborhood size above which a repair is classified `cascade`. The counter `epistrel_repairs_total{rung,class}` counts the corrected beats.

## Commitments

A structured story with `commitment.enabled` tracks what its prose leaves undecided. A reference with an unknown part, such as "someone knocks at the door", becomes a *thunk*: an open value nobody has decided yet. When the prose draws attention to it (salience), the reference also gets an entry in the *promissory ledger*, and the turn may not close until the entry is settled. Either a character's `think` or `do` beat depends on a value, which commits it for everyone, or the close commits the most generic fact the prose supports ("a knock occurred") and reports `ledger_minimal_commit`. Under `commitment.salience = rules_and_model` the `salience_detection` model purpose decides what is salient; under `rules` no model is called. Salience is optional work: when no budget is left, the turn treats every beat as background.

`thunks_max_open` (deployment setting, default 500) caps the open thunks a story keeps; past it, the oldest one that nothing drew attention to expires. `GET /stories/{id}/inspect/ledger?viewpoint=omniscient` lists the thunks and the ledger. The counters `epistrel_thunks_total{event}`, `epistrel_ledger_total{event}` and `epistrel_minimal_commits_total` count the lifecycle.

## Consolidation

A structured story with `memory.consolidation.enabled` compresses old episodic memories into standing facts and reflections in the background (the `consolidation` job and model purpose). Two engine-profile dials start a run, whichever is reached first: `memory.consolidation.window_memories` (default 50) uncovered episodic memories, or `memory.consolidation.cadence_turns` (default 20) turns since the last consolidated turn. A window holds at most `window_memories` memories, and every fifth window is folded again into higher-level reflections.

The `consolidation` purpose inherits the deployment's `default` model unless it has a `model_config` row of its own, and the Lite rolling summary (`lite_summary`) now inherits from `consolidation` before `default`: configuring a consolidation model moves the rolling summary onto it too, unless `lite_summary` has its own row.

`POST /admin/stories/{id}/consolidate {from_turn, to_turn, scope}` consolidates a window now, rerunning one already consolidated (the earlier outputs are kept, `superseded`). It answers 202 with the job, 409 `module_off` when the story has consolidation off, and 422 `scope_mismatch` when the window's private memories belong to characters no single output may serve. Without such a request a window is consolidated once: forks, undo and reconstruction reuse the stored outputs and make no model call.

## Quarantine, sanitize and author facts

Use the low-level calls under `/admin/stories/{id}` to correct a structured story's records directly. Like every `/admin` route, they need the service credential and a principal, and any principal may call them.

- `POST …/memories/{memory_id}/quarantine {reason}` takes a memory out of retrieval and consolidation, so no render's context holds it again. `DELETE` on the same path releases it. History and the story's events are untouched.
- `POST …/memories/{memory_id}/sanitize {span, surrogate}` replaces every occurrence of an exact span of a memory's text. The memory is re-indexed and re-embedded. The old text stays only in the `sanitize_original` audit event.
- `POST …/facts` commits an author fact (`provenance = authority = author`, optionally `locked`). `POST …/facts/{fact_id}/supersede` replaces one, and `POST`/`DELETE …/facts/{fact_id}/lock` locks or unlocks one. `GET …/facts?viewpoint=omniscient` lists canon.

Each write bumps the story's timeline revision. A Lite story answers 409 `module_off`.

The output gate quarantines on its own as well. A memory that the contexts of `repair.implication_threshold` rejected renders held (default 2; `0` turns this off) is quarantined with reason `repeated_implication` by `system`, and the regeneration runs without it. An author pin can be quarantined the same way, and the turn then reports a `pin_quarantined` warning. Watch `epistrel_quarantines_total{reason}` and `epistrel_sanitizes_total`. Release a memory quarantined by mistake with the `DELETE` above.

The nightly `redteam.yml` workflow runs R-01's 20 poisoning sequences (`-m "redteam or redteam_long"`). A red run opens a "Nightly red-team run failed" issue.
