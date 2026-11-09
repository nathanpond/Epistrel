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
5. Start Epistrel. Before it serves a request, it compares the ledger file with the restored database. Every erasure the file holds and the database lacks becomes **pending**, and the story it was made in is `blocked_by_erasure`. Every read and write of a blocked story is 503 `erasure_pending` (with `Retry-After: 30`), and so is `POST /admin/erasure` naming it, dry run or not. While a catalog erasure is pending, everything its reapply will touch is held the same way: a call naming the erased catalog character or persona, its companion scenario, a catalog character, scenario, persona, media asset or upload that names or depicts the subject, a stored result of `GET /operations/{id}` (or an `Idempotency-Key` replay) that names it, a global or rating policy whose text names it (`GET`, `PUT` or `DELETE /admin/policies/{id}`), a story created from any of them, or another catalog-scope erasure, dry run or not, is 503 `erasure_pending` (reads and writes alike, deletes included). Catalog lists and searches, `GET /personas`, `GET /media` and `GET /admin/policies` leave those entries out, `GET /admin/config-changes` leaves out a change whose values name the subject, and `GET /principals/{id}` lists them with a `null` name. A story's `GET /stories/{id}/settings` lists such a policy with a `null` name (it is still enforced), and a turn or write that breaks it is 503 `erasure_pending` instead of 422 `guardrail_violation`. An undelivered feedback export's content is 503 `erasure_pending` too, and the export stays undelivered until the reapply has redacted it. `GET /stories` still lists it with its status, and `DELETE /stories/{id}` still deletes it. Epistrel then reapplies each pending erasure itself, catalog erasures first, in the background (10 minutes at start-up, then every 5 minutes until done), and unblocks each story when nothing of it is pending. `GET /admin/erasure/status` shows what is pending. `POST /admin/erasure/reconcile` runs the reconciler at once. `/health` reports `checks.erasure_pending` and is `degraded` while it is above zero.

**A restore plus the ledger's reapplication reproduces the post-erasure state, never the pre-erasure one.** Restoring a backup taken before an erasure does not bring the erased person back: no restored story is served until the ledger has been reapplied to it, and afterwards its texts read `[erased]` where they named the subject, with an erasure marker where each deleted record was. The history as it was before the erasure cannot be rebuilt from a backup.

A reapply that fails (for example, `EPISTREL_CONFIG_KEY` is unset, so the erasure's keyed hashes cannot be made) leaves its story blocked. The error class and the attempts are in `GET /admin/erasure/status`, and the 5-minute retry keeps trying.

## Story snapshots and restore

A story's snapshot is its export, and its restore is an import: always a **new** story, never an overwrite (FR-STORE-7, FR-ADMIN-4). Whole-deployment backup and restore are [Backups](#backups) and [Restore](#restore) above.

- **Snapshot.** `POST /admin/stories/{story_id}/export` queues an export (202); `GET /admin/stories/exports/{job_id}` reads its state and `GET /admin/stories/exports/{job_id}/bundle` downloads the archive once it is ready. Archives are written under `EPISTREL_BUNDLE_DIR` and kept for `bundle_export_ttl_hours` (24 by default; `PUT /admin/settings`), then the retention sweep deletes them.
- **Restore.** `POST /admin/stories/import` takes a `multipart/form-data` body: part `metadata` (JSON: `owner`, `name`, `content_rating_id`, `engine_profile_id`, `upgrade_mode`, `ignore_warnings`), then part `file`, the archive. The manifest, the format, the rating (the one named, else the one whose name matches the bundle's: none is 422 `rating_required`) and the profile's impact report are checked before anything is created. A report that warns, without `ignore_warnings`, is answered `{applied: false, impact}` and nothing is created. Otherwise the call answers 202: the story exists with `status: importing` (every route of it answers 409 `story_importing` until its import ends), and a background job imports it. `GET /admin/stories/imports/{job_id}` reads its state, stage and progress, and, once it ends, its error and report.
- **The import's gates**, in order, before the story is served:
  1. this deployment's erasure ledger is applied to it (story-scope entries of the bundle's source story or the story it was forked from, and catalog-scope entries whose subject it references), and every imported text is checked against it;
  2. its whole active history is checked against the chosen rating's guardrails, the global policies and the minors policy among them; any violation refuses the import, with every violation listed (`rating_check_failed`), and inactive turns stay unchecked until a swap, an untruncate or a fork would reactivate them;
  3. every incomplete staged turn is closed, and every outbox row with no terminal outcome is parked (`imported_unsettled`): never dispatched, listed by `GET /stories/{id}/reconciliation`, and resolved only by the author's `POST /stories/{id}/reconciliation/{outbox_id}/resolve` (the row stays `imported_unsettled`, and `GET /stories/{id}/outbox` shows its `resolution`: kind, time and operation, #777);
  4. the profile is attached; a data-producing capability it adds over the bundle's starts a backfill as the story leaves `importing` (the story is then `paused`), unless `upgrade_mode` is `forward_only`.

  A refusal at any stage deletes the story and everything it wrote; the import's record stays, `refused`, with its error.
- **The import's limits** (deployment settings, `PUT /admin/settings`), checked while the archive decompresses:

  | Setting | Default | Bounds |
  |---|---|---|
  | `bundle_max_uncompressed_bytes` | 4294967296 | 1048576–68719476736 |
  | `bundle_max_entries` | 100000 | 10–10000000 |
  | `bundle_max_entry_bytes` | 1073741824 | 1048576–68719476736 |

- **The erasure boundary.** A delivered bundle is outside the erasure boundary (an erasure cannot reach a copy outside the deployment); a stored export not yet expired is inside it (a story-scope erasure expires and deletes it). Importing a bundle made before an erasure never brings the subject back: the import applies the ledger first.
- **Media.** An imported story's media are new assets of its owner. They skip the upload moderation hook: they arrive through an operator endpoint, and the import's rating check covers the story's active content.

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

### A story character named in promoted catalog drafts: erase in two steps

Promoting a story character into the catalog (`POST /stories/{id}/characters/{cid}/promote`) copies story text into catalog records: the character's definition and the seeded memories of its companion scenario. That text may name **another** character of the story, one who exists only there. A story-scope erasure never touches the catalog, so erase such a character in two steps (#504, ADR-176), **catalog first**:

1. Run the story-scope erasure as a dry run: `{subject: {kind: character, id}, scope: {story: <id>}, dry_run: true}`. Its `promoted_drafts` lists the catalog characters promoted from the story's characters, with their companion scenarios. Each comes with `follow_up`, the catalog-scope request to send.
2. Send that request: `{subject: {kind: character, id, story: <id>}, scope: {catalog: true}}`, after its own dry run if you want the counts and candidates. It reads the character's names from the story and redacts them from every catalog record. It touches no story.
3. Send the story-scope erasure.

The order matters. The story erasure removes the names the catalog erasure reads, and the ledger keeps only their keyed hashes. A catalog request sent after the story erasure is 422 `unknown_erasure_candidate`, and any mention left in the catalog has to be edited by hand.

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
- images that declare a subject or mention it in their prompt, caption or alt text, including story images and image candidates (both deleted; a story image's asset and bytes with it, unless another story's image still uses them), and the prompts of story image jobs that depict or name the subject (emptied; an unfinished job is cancelled)
- media assets that depict or name only the subject: under `erasure_shared_media = purge` (the default) the asset and its bytes are deleted; under `retire`, an asset another record still uses stays on disk, retired, with the subject removed from its metadata
- message feedback and its history
- feedback exports not yet downloaded (the subject is redacted in their cases)
- story exports not yet expired (a story-scope erasure expires and deletes them)
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

### Escalation yield and gate tightening

Every entailment call of a committed structured turn leaves an `escalations` row: the gate reason, the predicate shape (`layer|cardinality|temporal|polarity`), the deterministic and model confidences, the tentative verdict (`uncertain`), the model's verdict, whether the model flipped it, how the claim ended and the consequence (`repair_kept`, `repair_discarded` or `none`). The rows hold ids and codes only, follow their turn on the timeline, are not copied by a fork, and are pruned after `escalation_retention_days` (deployment setting, default 90). `GET /admin/evals/escalation-yield?from&to` (default the last 30 days) reports escalations, flips, the flip rate and the consequence-weighted flip rate (`repair_kept` 1, `repair_discarded` 0.25, `none` 0) per gate reason, per shape and per both, and the overall escalation rate with its 0.2 reference baseline (`is_gate: false`: it is a reference, not a gate). The counters `epistrel_verify_escalations_total{gate_reason,shape,flipped}` and `epistrel_verify_shadow_total{shape,agreed}` mirror it. A turn's `eval_hooks` (raw render event ids, verdicts, repair rungs, calls by purpose, latency by stage) is returned by `GET /operations/{id}?viewpoint=omniscient`.

To ask the model less often for one shape, propose a threshold in shadow: `PUT /admin/verify/gate {"shape": "...", "threshold": 0.4, "mode": "shadow"}`. The live gate does not change; each escalation of the shape also records whether the tightened gate would have escalated and, when it would have skipped the call, whether skipping agreed with the model. `POST /admin/verify/gate/{shape}/promote` (the shape with `|` written `~`) puts it live only when the shadow window (since the proposal) holds at least `gate_promotion_min_cases` (default 200) escalations and at least `gate_promotion_min_agreement` (default 0.98) of the would-have-skipped ones agreed; otherwise it is 409 `gate_not_promotable` with the numbers. A repeat `PUT` restarts the window. `"mode": "live"` applies a threshold at once (for a hard cost ceiling) and needs `"ignore_warnings": true`. `DELETE /admin/verify/gate/{shape}` removes it. Promotion, a live write and a removal of a live threshold are recorded as `verify_gate_threshold:<shape>` configuration changes; the next turn's gate uses them.

## Claim detection

A structured story looks for every claim a render makes before it checks any (FR-VERIFY-13,14). Each render, and the final composed message, passes through the declared structure (what its beats declared), deterministic detectors (English word tables for possession, transfer, consumption, injury and movement, quantities, time expressions, uncovered mentions, and the names and values hidden from the speaker), and, at `verification.level` `extraction` or `entailment`, the `extraction_backstop` model purpose. `verification.extraction_mode` decides when the model runs: `gated` (the default) only on the beats a detector flagged, or that declared nothing; `always` on every render (the `Structured (M4)` profile sets it). When the model is unreachable or its answer stays invalid, the render keeps the deterministic layers and the turn reports `extraction_degraded`.

Under `gated`, a share of renders (`extraction_shadow_sample_rate`, deployment setting, default 0.1) is a shadow sample: after the turn commits, the `extraction_shadow` job runs the extraction over the beats gating skipped, under its own budget, and records the claims it would have missed. The samples are pruned after `extraction_shadow_retention_days` (deployment setting, default 90). `GET /admin/evals/extraction-shadow` sums them over a window (`from`, `to`, default the last 30 days; optional `story_id`): samples, missed claims by category, the miss rate, and, apart, how many claims were detected and how many were checked. `protected_index_max` (default 5,000) caps the hidden names and values one turn matches; past it the turn report says `protected_index_truncated`. The counters `epistrel_claims_detected_total{layer,category}` and `epistrel_claims_checked_total{verdict,category}` count detection and checking as separate series: every detected claim is checked, while how many claims are detected at all is the measured rate.

## Repair

A structured story whose profile has the `repair` module on (`repair.max_attempts` of 1 or more; `Structured (M4)` sets 1, the default is 0) repairs every rendered beat whose claims are contradicted, undecided under the `repair` safe disposition, or unpermitted novelty (#151). The ladder tries, in order: dropping the sentences that carry the claims, swapping a functional claim's object for canon's, regenerating the beat through the `regeneration` purpose (which shares the generation model unless it has its own `model_config` row), and the stored safe fallback. Every correction passes the output policies and has its claims detected and checked again before it is accepted. A regeneration starts only when the turn's budget can reserve its render, its extraction and one re-check per model-assisted output policy; otherwise the turn reports `repair_deterministic`, and a fallback reports `safe_fallback`. The rejected texts are `render_rejected` audit events (with the beat's `verify_verdict`), kept for the audit retention like every audit record; `turn_report.repairs` lists each repair's rung, class and neighborhood. `cascade_threshold` (deployment setting, default 5) is the neighborhood size above which a repair is classified `cascade`. The counter `epistrel_repairs_total{rung,class}` counts the corrected beats, by the rung accepted (`narrowing`, `substitution`, `regeneration`, `safe_fallback`) and the class (`cosmetic`, `substitution`, `cascade`).

## Commitments

A structured story with `commitment.enabled` tracks what its prose leaves undecided. A reference with an unknown part, such as "someone knocks at the door", becomes a *thunk*: an open value nobody has decided yet. When the prose draws attention to it (salience), the reference also gets an entry in the *promissory ledger*, and the turn may not close until the entry is settled. Either a character's `think` or `do` beat depends on a value, which commits it for everyone, or the close commits the most generic fact the prose supports ("a knock occurred") and reports `ledger_minimal_commit`. Under `commitment.salience = rules_and_model` the `salience_detection` model purpose decides what is salient; under `rules` no model is called. Salience is optional work: when no budget is left, the turn treats every beat as background.

`thunks_max_open` (deployment setting, default 500) caps the open thunks a story keeps; past it, the oldest one that nothing drew attention to expires. `GET /stories/{id}/inspect/ledger?viewpoint=omniscient` lists the thunks and the ledger. The counters `epistrel_thunks_total{event}`, `epistrel_ledger_total{event}` and `epistrel_minimal_commits_total` count the lifecycle.

Character beliefs change only through typed transitions (#160): `epistrel_belief_transitions_total{kind}` counts them by kind, and `epistrel_belief_transitions_refused_total` counts the refused ones (an antecedent that does not precede its step, an invalid antecedent, or a level its scalar contradicts). `POST /admin/stories/{id}/beliefs` sets a character's belief as an author act (409 `module_off` unless the story keeps partitions).

With per-character partitions and perception at `co_location` or `full`, a structured turn routes each beat to the characters who perceive it (#161): who is where, which places adjoin and through what barrier, and who is on a remote channel come from canon (`located_at`, `adjacent_to`, `on_channel`), never from the request's scene roster, and each perceiver gets their own memory of the beat (the words, or only its envelope). A render's eavesdrop or remote call that the topology cannot support is dropped rather than honored; `epistrel_perception_repairs_total{reason}` counts those repairs (`no_path`, `sealed_barrier`, `no_channel`, `unknown_character`, `target_absent`, `unknown_location`), and the turn's report lists them under `perception`.

When a character speaks, every character who heard the words records that it was said, and what each one makes of it is a separate testimony transition (#162): trust in the speaker, what the hearer already knows and whether the remark was a joke decide it. Salient hearers may be adjudicated by the `belief_adjudication` model purpose (it defaults to the entailment model) as optional work within the turn budget: `epistrel_adjudications_total{result}` counts those calls (`adjudicated`, `kept`, `defaulted`, `unavailable`) and `epistrel_testimony_total{outcome}` the outcomes written (`accept`, `reject`, `suspend`, `speech_act_only`). A turn whose budget could not fit an adjudication lists `testimony_defaulted` in its degradation, and its report summarises the turn's testimony under `epistemic.testimony`.

Under per-character partitions a person the story's characters interact with (a wave, a greeting, a tip handed over, a vocative) or that the salience step marks as a character is modeled as a `minor` character (#163): a story character with a sparse memory of only the interactions it takes part in, a smaller consolidation window and hot budget, and no default render. Its true age comes from an optional age-repair call; a turn that could not fit it lists `modeling_deferred` and the person waits for a later interaction. `epistrel_characters_modeled_total{trigger}` counts the characters modeled (`interaction`, `salience`), and the turn report lists them under `modeling`. The deployment setting `minor_hot_budget_fraction` (default 0.2) is the share of the per-character hot budget a minor gets. An author promotes a character with `POST /stories/{id}/characters/{cid}/weight` and models an entity directly with `POST /stories/{id}/entities/{eid}/model`.

A character can only know what it had a way to learn (#165). Under per-character partitions, when a turn settles something an earlier turn left open (who knocked) on an existing character, the engine checks that character's later lines. If they sincerely asked about it ("who was at the door?") or said something it contradicts, the binding is rejected and steered elsewhere (`turn_report.commit.rejected_bindings[].conflicting_lines`). Otherwise the character's knowledge of the act is recorded from the time it happened, with a memory of it. When the character was there and had no record of it, the resolution is flagged, not refused: `could_have_known_flags` in the turn report (severity `mild`, `moderate` or `strong` by the story-time gap), `could_have_known_flag` in its degradation, the thunk's entry on `GET /stories/{id}/inspect/ledger`, and a steer on the next render. `epistrel_could_have_known_checks_total{outcome}` counts the checks (`known`, `no_path`, `not_applicable`) and `epistrel_could_have_known_flags_total{severity}` the flags.

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

## Images

Every image a deployment stores says where it came from and how far Epistrel checked it. Each media read returns a `source` (`generated` from an Epistrel-built prompt, `generated_from_consumer_prompt`, `uploaded`, or `extracted` for a video's poster) and a `check_status`:

- `passed` comes with a `check` block. It names the image-check model and its endpoint's host, and lists each applicable image-output policy with its result and whether that checker is certified for that policy. Certification is per policy (NFR-QUAL-12). A record in `image_check_certifications` for the checker's fingerprint and the policy makes it `certified: true`. #270 writes those records from E-11's runs, and until then every result reads `certified: false`.
- `unchecked` comes with `check: null`.

`unchecked` means Epistrel did not check this image; it carries no rating or viewpoint assurance from Epistrel and is never a pass.

A `passed` image carries only the measured assurance of its checker (NFR-QUAL-12): the output check is a backstop with measured error rates, not an assurance that every image is safe.

**Story images (FR-IMG-8).** A generated story image is made only from information its audience viewpoint perceived, and the image model is never given anything else. Images are not inspected pixel by pixel, so an image model can still produce unperceived content by coincidence. A consumer-supplied prompt carries only the protected-term match, and an uploaded image carries no viewpoint guarantee.

A story image is generated on request (`POST /stories/{id}/images` with a JSON body) at a position, for one audience viewpoint, optionally framed by a camera POV. Without a `prompt`, Epistrel builds the prompt and picks the reference images from what that viewpoint perceived there: its filtered beats of that turn (under Lite, the message the player was shown), its own location as it holds it, and the characters it perceives as present, each by perceived appearance and apparent age band. A character whose identity the viewpoint does not know contributes none of its visual identity, seed or reference images, and `restricted` elements reach only `omniscient` images. The prompt, negative prompt, caption and alt text pass the viewpoint's protected-content match (no name, alias or protected term it does not hold, and no words of a beat it did not perceive; 422 `viewpoint_protected_content`) before the image-prompt policies, the reservation and the job; the job stores the prompt and sends exactly that. Such an image is stored `source: generated`, `viewpoint_basis: filtered_inputs`. A consumer's own `prompt` is sent as written, followed by the scenario's visual style and the rating's image guidance, and is stored `generated_from_consumer_prompt`, `viewpoint_basis: protected_term_match`. An upload is `uploaded`, `viewpoint_basis: none`.

**Configuring the checker.** The `image_check` model purpose needs a vision-capable model. It has no default and never inherits the `default` row: `PUT /admin/model-config/image_check {model, base_url}` plus its API key. One call checks an image against every model-assisted policy that applies to image outputs. The call carries the image (downscaled to at most 1024 px), its prompt, caption and alt text, each policy's text, and the subjects as opaque labels with their true ages. It never carries story text. Deterministic policies run locally on the prompt, caption and alt text. An uncertain verdict follows the policy's `on_uncertain`: `violation` blocks the image, and `pass` stores it with `uncertain: true`. The seeded minors policy blocks. The call uses the purpose's timeout (default 60 s) and the structured-call retries.

Without a checker, generation is refused up front while any image-output policy applies (409 `image_check_unconfigured`; the seeded minors policy always applies). Uploads still work, and they are stored `unchecked` after the moderation hook, which is then the control. With a checker, an uploaded image (and a video's poster) is checked in staging before the moderation hook. A violation is 422 `guardrail_violation` and writes an `image_upload_rejected` audit row (the policy ids, never the image or its hash). A checker that cannot answer is 503 `image_check_unavailable`. Nothing is stored in either case. A generated image that fails is deleted from staging and generation retries within `image_retry_budget`.

The checker fingerprint is the first 12 hex digits of the SHA-256 of the canonical JSON of the endpoint's host, the model id, the version the model last reported, and the SHA-256 of the `image_check` prompt template. Changing any of them, including editing the template, makes the checker uncertified until it is certified again.

**Certifying a checker (NFR-QUAL-12, ADR-182).** A checker is certified per policy, by a record in `image_check_certifications` for its fingerprint (`epistrel models fingerprint --purpose image_check` prints it). For any image-output policy except the seeded minors policy, the record is an `eval_run`: one policy's row of an E-11 report (`uv run python -m epistrel.evals run --suite e11`) that passed both its false-negative and false-positive floors (`image_check_floors`, `{policy_id: {max_fn, max_fp}}`, empty by default) on an owner-approved, fully reviewed set. The run writes these records under `src/epistrel/core/seeds/certifications/image-check/`, and the start-up loader records the committed ones. The minors policy (`external_evidence_only`) is never measured for false negatives, because Epistrel holds no violating images. A checker is certified for it only by recording the checker vendor's published evaluation (`POST /admin/certifications/image-check`, `evidence_kind: external_attestation`: title, https URL, publisher, publication date and the quoted figure); otherwise it is an uncertified backstop for that policy. `POST /admin/certifications/image-check/{id}/invalidate` withdraws a record. The newest valid record decides for checks made afterwards, and an image already checked keeps the flag it was checked with. `GET /certifications?subject=image_check` lists the records.

**Story image revalidation (FR-IMG-4).** A generated story image is attached only if its story still holds when the job promotes it. Inside the attaching transaction, under the story's lock, the job checks that the story still exists and that the image's turn variant is still active. It also checks that no amendment made since the request changed that turn's beats or any fact about a depicted subject other than `age`. The erasure guard must refuse none of its subjects or texts, and the story's current image policies must pass the prompt and the image. Only the state at promotion counts, so a turn truncated and restored before then still gets its image. A failure ends the job `stale`, or `target_gone` for a deleted story, with the reason in `status_reason` and `status_detail` and an `image_job_ended` audit row. Nothing is attached, the staged bytes are deleted and the reservation is released. While a rating, reactivation or backfill check pauses the story, the job stays `checking` with `waiting_on: pause` and its staged image kept. It revalidates every `image_pause_recheck_s` (default 30 s, 5 to 3600) and ends `stale` with `paused_too_long` after `image_pause_max_wait_s` in total (default 86400 s, 60 to 604800). A policy that came into force during generation is evaluated outside any lock before the image is attached. An image that fails it is not generated again. `epistrel_image_revalidation_total{outcome}` counts the promotions' revalidations.

**Automatic illustration (FR-IMG-6).** A story whose `illustration_mode` is `auto` gets a picture at the points its turns' renders mark: a new location, a character's first appearance, or a moment the scene turns on. Lite stories get it too, at no extra model call: the prompt asks the model to end its reply with a final `[[illustrate: <reason>]]` line, which is always stripped before the reply is checked, stored or shown. Up to `max_auto_images_per_turn` points per turn (0 to 4, default 1) are queued in the turn's own commit as `story` image jobs owned by `system`, keyed `auto:<operation_id>:<k>`. A staged turn queues them when its completion stage commits. The respond response lists them as `pending_images`. Each job runs like a requested one, except that it builds its prompt, runs the checks and reserves inside the job. With `image_check` unset it ends `failed`/`image_check_unconfigured` before any of that. A refused prompt ends it `failed`/`guardrail_violation`, and an exhausted deployment-day limit ends it `cap_reached`. None of these affect the turn. No job is queued without an image adapter or with a cap of 0. Stopping automatic illustration takes `image_limit_deployment_day = 0`, an unconfigured adapter, or the story setting. `epistrel_image_auto_jobs_queued_total{pipeline}` counts the queued jobs, and `epistrel_image_auto_points_dropped_total{reason}` counts points dropped as beyond the cap (`cap`) or as being on a beat the turn did not commit (`unmapped`). `epistrel_image_auto_hints_dropped_total` counts invalid hints.

**Limits (FR-IMG-11).** Epistrel counts output images, every image a provider returns, against two daily limits. Both are deployment settings:

- `image_limit_deployment_day` (default 200) counts every job;
- `image_limit_entry_day` (default 16) counts a catalog entry's jobs.

A day is the local date in `deployment_time_zone` (an IANA zone, default `UTC`). Each limit is a whole number from 0 to 100000, and 0 turns generation off at that scope. There is no unlimited value, and no per-story or per-turn limit: per-story and per-turn admission is the consumer's to manage. `catalog_candidates_n` can never exceed a non-zero `image_limit_entry_day`.

Every attempt reserves its outputs before the provider call. A request that does not fit is refused at enqueue: 409 `image_limit_reached` names the scope, limit, usage and `resets_at` (the next local midnight), and 422 `images_disabled` names a scope set to 0. A retry that does not fit ends its job `cap_reached`, with `status_detail {scope, period}`. Outputs beyond the requested `n` are counted (`overage`, `extra_outputs`) and discarded unchecked.

The counters are stored sums of the reservation records. The daily `image_counter_reconcile` task compares them. It reports drift through `epistrel_image_counter_drift_total{scope}`, `/health`'s `checks.image_counters: drift` and the logs, and it never rewrites anything. `epistrel images reconcile` runs the same check (exit 1 on drift). `--fix` rewrites drifted counters, releases `held` records whose job has ended, and writes an `image_counters_reconciled` audit row.

Records and counters are deployment accounting. They survive a story's deletion, with `story_id` nulled, and they never travel in a bundle. The daily `image_accounting_retention` task deletes them once their day has been closed for `image_accounting_retention_days` (default 90) and no `held` record names them.

**Late returns (FR-IMG-11).** A worker that stalls past its lease is fenced. The reaper advances the job's attempt token, and the stalled worker can then only settle its own attempt: it never attaches a picture. No new attempt for the job starts before the stalled call's horizon, `started_at + request_timeout_s + image_attempt_grace_s` (grace default 60 s, 0 to 3600), and `GET /image-jobs/{id}` shows `waiting_for_horizon: true` until then. At the horizon a record still held is released with cost `unknown`. A late result is always counted and costed, and never shown:

- before the horizon it settles inside the attempt's reservation (`late_discarded`);
- after it, every output it returns counts as used and overage (`late_after_horizon`), so usage can pass a limit by the outputs that really came back, and later requests that day are refused;
- a second delivery changes nothing (`late_duplicate`), except that it fills in a cost that was still unknown, once.

With an adapter that declares `confirms_completion`, the job does not retry at the horizon. It cancels the old call once and asks its status every `image_status_poll_s` (default 15 s, 5 to 600, and never above the wait). If the provider says `succeeded`, the job adopts those outputs and pays nothing twice. If it says `failed` or `cancelled`, the job retries. If there is no definitive answer within `image_confirm_wait_max_s` of the horizon (default 1800 s, 60 to 86400), the job retries anyway and reports `confirm_fallback: true`. Each attempt's `outcome`, `requested`, `used`, `overage`, `extra_outputs`, `cost`, `send_state`, `basis` and `resend_of` appear in the job's `attempts`, and `epistrel_image_attempt_outcomes_total{outcome}` counts the outcomes.

**Ambiguous sends (FR-IMG-11).** Every provider call is recorded as `sending` before it is made, with an idempotency key derived from the job and the attempt. If a worker is lost before the provider's request identifier or result is recorded (a `sync` adapter's whole call, or an `async` adapter's `submit` before its acknowledgement commits), Epistrel cannot tell whether the provider generated anything. What happens next depends on the adapter:

- With `idempotency_keys`, the job re-sends the same request with the same key at once, at most twice per key. The outputs count once, on the original attempt (`resent`), and are checked and promoted as usual. A provider that has forgotten the key, or does not really deduplicate, can bill twice.
- Otherwise the attempt's `send_state` becomes `ambiguous`. Its reservation is held to its horizon and then counted as **presumed**: every requested output counts as used, with cost `unknown`, so the limits err toward generating less. The job then retries, which the provider may bill again. If the real result reaches Epistrel later, it corrects the presumption once (`basis: corrected`, `late_after_presumed`), and the counters move by the difference. A job that has already ended, even `cap_reached` by the presumed usage, stays ended.

The bundled OpenRouter adapter does not declare `idempotency_keys`: the recorded fixtures show OpenRouter billing a repeated key again. So a worker lost mid-call there is an ambiguous send. Each presumption and each re-send counts against `job_max_attempts`. Ambiguous sends are the records with `send_state = ambiguous`. A result whose records accounting retention has already deleted is logged and counted as `late_unaccounted`, and changes no counter; its cost is still recorded, on a cost row marked `late_unaccounted`.

**Cost (FR-IMG-11).** Every provider attempt that reached the provider has one cost record, written when the attempt resolves. Billed outputs cost what they cost whatever became of them: kept, discarded by a check, stale, an unattached candidate, or a late or duplicate return. An attempt the provider refused before generating (HTTP 400, 401, 402, 403, 413 or 429) is recorded at `zero`. Anything else without a figure (a 5xx, a timeout, an ambiguous send, a release at the horizon, a success with no cost) is `unknown`: counted, never added as 0, and never estimated from the endpoint's pricing. An unknown cost is filled in once if a later return reports it, and stays on its original day. An idempotent re-send's cost is on the attempt it re-sent, so it counts once. Amounts are kept to 8 decimals in the provider's currency, and totals are per currency, never mixed or converted. The reports:

- `GET /admin/image-usage?from=&to=` gives the deployment's usage (`outputs_used`, `overage`, `extra_outputs`, `reserved`), `ambiguous_sends`, `cost_unknown_attempts`, `cost_zero_attempts` and `costs` per currency, for every local day of the range and in `totals`. `from` and `to` are local dates in `deployment_time_zone`, both inclusive; the default is the last 30 days through today, at most 366 days, and `from` after `to` is 422 `invalid_range`. A cost lands on the day its attempt was reserved, even when it settles after midnight. Days the accounting retention purged are left out, and `retained_from` says where the report starts.
- `GET /stories/{id}/image-usage` gives a story's totals all-time and its active days, from its attempts' records. It has no limits: per-story admission is the consumer's.
- `GET /catalog/characters/{id}/image-usage` and `GET /catalog/scenarios/{id}/image-usage` give an entry's, with today's `image_limit_entry_day`, used, reserved and remaining.
- `GET /image-jobs/{id}` gives each attempt's `cost`, `cost_status`, `currency`, `outputs_billed` and `accounting`, the job's `cost` totals and its `usage` (`outputs_used`, `overage`, `extra_outputs`, `ambiguous_sends`).

The cost records hold ids, amounts, currencies, counts and dates, and no story content. They survive erasure and principal offboarding. A story's deletion keeps them with the story removed, so the deployment report still counts them, and the story's report then answers 404. They are never exported, and they go with the accounting retention (`image_accounting_retention_days`), by the day they report on.
