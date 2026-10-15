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
5. Start Epistrel. Before it serves a request, it compares the ledger file with the restored database. Every erasure the file holds and the database lacks becomes **pending**, and the story it was made in is `blocked_by_erasure`. Every read and write of a blocked story is 503 `erasure_pending` (with `Retry-After: 30`). `GET /stories` still lists it with its status, and `DELETE /stories/{id}` still deletes it. Epistrel then reapplies each pending erasure itself, catalog erasures first, in the background (10 minutes at start-up, then every 5 minutes until done), and unblocks each story when nothing of it is pending. `GET /admin/erasure/status` shows what is pending. `POST /admin/erasure/reconcile` runs the reconciler at once. `/health` reports `checks.erasure_pending` and is `degraded` while it is above zero.

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
- the subject-reference index
- a backup restored after the erasure: its stories are not served until the erasure ledger, kept outside the database and copied into every backup, has been reapplied

**Out of scope.** An erasure cannot reach:

- exports already delivered
- data retained by external model/image/data providers (including hosted guardrail or image check providers)
- subjects appearing in images only visually with no declared or named reference
- backups taken before the erasure, until restored and reconciled
- logs, which never carry story content
- mentions the matcher classes as ambiguous (a name word that is also a common word, capitalised at the start of a sentence) unless the operator confirms them, and nicknames never declared as aliases; an erasure reports how many candidates remain (`candidates_remaining`). A word of the subject's name on its own is not matched when another character in the same scope also uses it (a shared surname) or when it is a title or particle (`Dr`, `de`); a common word (`will`, `rose`) is matched only capitalised (ADR-154)

**Logs.** Operational logs contain no story content by default (NFR-SEC-4).
<!-- erasure-boundary:end -->

The same statement is served by `GET /admin/erasure/boundary` as data, and it ends the API description (`/openapi.json`, `info.description`). A test keeps the three equal.

## Feedback exports

`POST /admin/feedback/export` (FR-FB-4) writes rated messages as labelled cases, one JSON Lines file per export, only while the deployment setting `feedback_export` is on (it is off by default; `PUT /admin/settings {"feedback_export": true}`). The file is kept in the blob store at `exports/<export id>.jsonl` (the media root, or the S3 bucket) for seven days, then the media sweep deletes it; `DELETE /admin/feedback/exports/{id}` deletes it sooner. It is served by `GET /admin/feedback/exports/{id}/content` behind the service credential only. Every export stays recorded in `feedback_export_log` (who, when, the window and filters, the case count), also after its file and its stories are deleted; `GET /admin/feedback/exports` lists them. A case holds the message, the player input, its position, the configuration it was produced under, and the rating with its tags and text: never a principal, a credential or a setting.

<!-- feedback-export-boundary:start -->
Delivered exports are outside the erasure boundary (FR-ADMIN-8: exports already delivered): an export's file counts as delivered once it has been downloaded, and an erasure cannot reach a copy outside the deployment. Until its first download, an erasure redacts the subject in the file's cases.
<!-- feedback-export-boundary:end -->
