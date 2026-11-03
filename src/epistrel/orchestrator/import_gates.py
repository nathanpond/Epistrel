"""The import gates (FR-STORE-7's safety, FR-ADMIN-7,9, FR-RATE-7, FR-EXT-22, FR-PROF-7,8;
#251): what an imported story must pass before it is served.

They run in ``core.bundles.import_job.IMPORT_GATES``'s slot, in this order, while the story is
still ``importing``; any refusal (``ImportRefused``, or the erasure fence's
``erased_subject_referenced``) leaves nothing (``refuse_and_clean``).

1. ``apply_erasure_ledger`` (stage ``erasure``): every ``erasure_ledger`` entry that concerns
   the imported story is applied to it, catalog scope first, then in ``at`` order:
   story-scope entries of the bundle's source story (or of a story it was forked from, as its
   genesis records), their subject translated through the import's id map; and catalog-scope
   entries whose subject the imported rows reference structurally (a character's
   ``source_catalog_id``, a player's ``persona_id``), run as a story-scope erasure of that
   story character. Each re-runs #124's ``erase`` with the entry's own id, principal ``system``
   and no ledger line, as #125's reapply does, with the entry's confirmed candidates (ADR-154)
   translated through the id map (``_translated_candidates``); a subject the bundle no longer
   holds (a post-erasure bundle) is nothing to apply. The entries are then copied to the new
   story with ``copied_from`` set (as a fork copies them, #127), and the guard checks every
   imported text against them (``guard.check_content``), which must find nothing. Without the
   deployment's erasure key the gate cannot run and the import is refused.
2. ``check_guardrails`` (stage ``guardrails``): the whole active history checked against the
   chosen rating's effective guardrails (global, the rating's, the story's and its characters',
   the minors policy among them) with #118's evaluator, exactly as a tightening checks it, every
   violation listed: ``rating_check_failed``. When everything passes, every active turn variant
   is stamped with the set evaluated. Inactive imported turns keep no stamp (#250 never imports
   it), so a swap, untruncate or fork that would reactivate one checks it first (#120).
3. ``park_outbox`` (stage ``outbox``): every imported incomplete staged turn is closed as a fork
   closes one (FR-EXT-19); every imported outbox row with no terminal outcome is parked
   (``imported_unsettled``, never executable) with a ``reconciliation_marks`` row naming the
   source row; a held reservation on a State with ``reservation_expiry_minutes`` gets its
   expiry timer again, due that many minutes after the import.
4. ``attach_profile`` (stage ``profile``): the chosen profile is already the story's (the
   request's impact report decided it); a data-producing capability it adds over the
   configuration the bundle ran under, with a coverage gap over the imported turns, starts a
   gap-fill backfill as the story leaves ``importing``
   (``upgrade_mode`` ``backfill``, the default: the story is then ``paused``), or keeps the gap
   (``forward_only``).

Log lines carry ids and counts only (invariant 3).
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import timedelta
from typing import Any

from sqlalchemy import and_, insert, or_, select, update
from sqlalchemy.ext.asyncio import AsyncConnection

from epistrel.core import clock
from epistrel.core.bundles.import_job import ImportContext, ImportRefused, register_gate
from epistrel.core.enums import Membership, OperationKind, OperationStatus
from epistrel.core.ids import new_ulid
from epistrel.core.membership import active_operation
from epistrel.observability.logging import get_logger

log = get_logger(__name__)

SYSTEM = "system"
SUMMARY_ONLY = frozenset({"rolling_summary"})
"""Capabilities a gap fill does not re-derive: a Lite rolling summary is built by its own job
(#72) over whatever the story holds."""
UNSETTLED_FROM = ("intended", "claimed", "unknown")
"""The outbox statuses with no terminal outcome: an imported row in one is parked."""


# -- 1. the erasure ledger -----------------------------------------------------------------------


async def _ancestors(conn: AsyncConnection, story_id: str) -> set[str]:
    """The stories the imported story's genesis says it was forked from (as recorded)."""
    from epistrel.core.tables import events

    found: set[str] = set()
    for (payload,) in await conn.execute(
        select(events.c.payload).where(
            events.c.story_id == story_id, events.c.type == "story_created"
        )
    ):
        origin = dict(payload or {}).get("forked_from")
        if isinstance(origin, dict) and origin.get("story_id"):
            found.add(str(origin["story_id"]))
    return found


async def _entries(conn: AsyncConnection, ctx: ImportContext) -> list[Any]:
    """The ledger entries that concern the imported story, catalog scope first, then ``at``."""
    from epistrel.core.tables import characters, erasure_ledger

    sources = {ctx.inserted.source_story_id} | await _ancestors(conn, ctx.story_id)
    ch = characters.c
    referenced = {
        str(value)
        for row in await conn.execute(
            select(ch.source_catalog_id, ch.persona_id).where(ch.story_id == ctx.story_id)
        )
        for value in (row.source_catalog_id, row.persona_id)
        if value is not None
    }
    e = erasure_ledger.c
    rows = list(
        await conn.execute(
            select(erasure_ledger).where(
                or_(
                    and_(e.scope == "story", e.story_ref.in_(sorted(sources))),
                    and_(
                        e.scope == "catalog",
                        e.subject_id.in_(sorted(referenced) or [""]),
                    ),
                )
            )
        )
    )
    unique: dict[str, Any] = {}
    for row in rows:
        # A fork's copy names the same erasure as its original: applied once.
        unique.setdefault(str(row.copied_from or row.id), row)
    return sorted(unique.values(), key=lambda r: (r.scope != "catalog", r.at, str(r.id)))


async def _subject_of(conn: AsyncConnection, ctx: ImportContext, entry: Any) -> dict[str, Any]:
    """The erasure's subject in the imported story."""
    from epistrel.core.tables import characters

    if str(entry.scope) == "story":
        return {
            "kind": str(entry.subject_kind),
            "id": ctx.inserted.ids.get(str(entry.subject_id), str(entry.subject_id)),
        }
    ch = characters.c
    column = ch.persona_id if str(entry.subject_kind) == "persona" else ch.source_catalog_id
    found = await conn.scalar(
        select(ch.id).where(ch.story_id == ctx.story_id, column == str(entry.subject_id)).limit(1)
    )
    return {"kind": "character", "id": str(found or entry.subject_id)}


async def _translated_candidates(ctx: ImportContext, entry: Any, request: Any) -> list[str]:
    """ADR-154 on import (#251): the entry's operator-confirmed candidates, translated to the
    imported story. A candidate id hashes its record's id (``redact.candidate_id``), which the
    import remapped, so a dry run of the erasure on the imported story lists the candidates it
    meets, and each whose source id (its record's id mapped back, the same field, start and
    rule) the entry confirmed is confirmed here. A confirmed id the bundle does not hold (a
    record the source wrote after the export, or a projection rebuilt with a new id) is
    skipped, as #125's reapply skips one."""
    from epistrel.core.erasure import erase as erasure
    from epistrel.core.erasure.redact import candidate_id

    counts = entry.counts if isinstance(entry.counts, dict) else {}
    source = {str(c) for c in counts.get("confirmed_candidates") or ()}
    if not source:
        return []
    reverse = {new: old for old, new in ctx.inserted.ids.items()}
    preview = await erasure.dry_run(
        ctx.db, request, compressor=ctx.services.extra["compressor"], principal=SYSTEM
    )
    return [
        c.candidate_id
        for c in preview.candidates
        if candidate_id(
            c.record_table, reverse.get(c.record_id, c.record_id), c.field, c.start, c.rule
        )
        in source
    ]


async def _empty_cards(ctx: ImportContext, catalog_id: str) -> None:
    """#382 on import: a catalog-scope entry, run as a story-scope erasure of the story
    character (which keeps the public card, name redacted), also empties the card cast from the
    catalog character in the imported story's genesis creation report, as the catalog-scope
    erasure did in every story's report it reached."""
    from epistrel.core.erasure.erase import catalog_erased_report
    from epistrel.core.tables import operations

    o = operations.c
    async with ctx.db.begin() as conn:
        found = await conn.execute(
            select(o.operation_id, o.turn_report).where(
                o.story_id == ctx.story_id, o.kind == OperationKind.GENESIS.value
            )
        )
        for row in list(found):
            value = row.turn_report
            report = value.get("creation_report") if isinstance(value, dict) else None
            if not isinstance(report, dict):
                continue
            emptied = catalog_erased_report(dict(report), catalog_id)
            if emptied != report:
                await conn.execute(
                    update(operations)
                    .where(o.operation_id == row.operation_id)
                    .values(turn_report={**value, "creation_report": emptied})
                )


async def apply_erasure_ledger(ctx: ImportContext) -> None:
    from epistrel.core.erasure import erase as erasure
    from epistrel.core.erasure import guard
    from epistrel.core.erasure.index import records_of
    from epistrel.core.errors import StoryNotFound, SubjectNotFound
    from epistrel.core.tables import erasure_ledger

    async with ctx.db.connect() as conn:
        entries = await _entries(conn, ctx)
    key = ctx.services.extra.get("config_key")
    applied = remaining = 0
    if entries and not key:
        raise ImportRefused(
            "erasure_key_missing",
            "the erasure ledger cannot be applied without EPISTREL_CONFIG_KEY",
            {"setting": "config_key"},
        )
    for entry in entries:
        async with ctx.db.connect() as conn:
            subject = await _subject_of(conn, ctx, entry)
        request = erasure.ErasureRequest.model_validate(
            {"subject": subject, "scope": {"story": ctx.story_id}}
        )
        try:
            confirmed = await _translated_candidates(ctx, entry, request)
            result = await erasure.erase(
                ctx.db,
                request,
                principal=SYSTEM,
                config_key=str(key),
                compressor=ctx.services.extra["compressor"],
                collect=ctx.services.extra.get("collect"),
                erasure_id=str(entry.copied_from or entry.id),
                write_ledger=False,
                confirmed=confirmed,
            )
        except SubjectNotFound, StoryNotFound:
            continue  # a post-erasure bundle: nothing of the subject left to apply
        if str(entry.scope) == "catalog":
            await _empty_cards(ctx, str(entry.subject_id))
        applied += 1
        remaining += int(result.candidates_remaining)
    async with ctx.db.begin() as conn:
        for entry in entries:
            story_scope = str(entry.scope) == "story"
            await conn.execute(
                insert(erasure_ledger).values(
                    id=new_ulid(),
                    subject_kind=str(entry.subject_kind),
                    subject_id=ctx.inserted.ids.get(str(entry.subject_id), str(entry.subject_id))
                    if story_scope
                    else str(entry.subject_id),
                    scope="story",
                    story_ref=ctx.story_id,
                    principal=str(entry.principal),
                    operation_id=entry.operation_id,
                    counts=dict(entry.counts or {}),
                    scan_term_hashes=list(entry.scan_term_hashes or []),
                    applied_at=entry.applied_at or clock.now(),
                    applied_note=entry.applied_note,
                    copied_from=str(entry.copied_from or entry.id),
                    at=entry.at,
                )
            )
        if entries:
            records = await records_of(conn, ctx.story_id)
            contents = [
                guard.Content(tuple(v for v in record.fields.values() if v)) for record in records
            ]
            await guard.check_content(
                conn,
                ctx.story_id,
                [c for c in contents if c.texts],
                base_revision=None,
                path=__name__,
            )
    ctx.report["erasure"] = {
        "entries": len(entries),
        "applied": applied,
        "candidates_remaining": remaining,
    }
    log.info("import erasure gate", story_id=ctx.story_id, entries=len(entries), applied=applied)


# -- 2. the guardrails ---------------------------------------------------------------------------


async def check_guardrails(ctx: ImportContext) -> None:
    from epistrel.core.config_changes import read_guardrail_settings, read_settings
    from epistrel.core.policies.model import ModelAssistedRule
    from epistrel.core.ratings import change
    from epistrel.core.ratings.stamp import live_policies, stamp_of
    from epistrel.core.tables import content_ratings, operations
    from epistrel.orchestrator.jobs.handlers import rating_check
    from epistrel.orchestrator.policies.store import GuardContext, read_cast

    async with ctx.db.connect() as conn:
        history = await change.read_history(conn, ctx.story_id)
        version = str(
            await conn.scalar(
                select(content_ratings.c.current_version_id).where(
                    content_ratings.c.id == ctx.rating_id
                )
            )
        )
        found = await live_policies(conn, ctx.story_id, ctx.rating_id)
        cast = await read_cast(conn, ctx.story_id)
        threshold = (await read_settings(conn)).minor_age_threshold
    context = GuardContext(policies=found, cast=cast, threshold=threshold)
    active = context.active
    models = ctx.services.extra.get("models") or rating_check.bound()
    if models is None and any(isinstance(p.rule, ModelAssistedRule) for p in active):
        raise ImportRefused(
            "rating_check_failed",
            "the rating's model-assisted policies cannot be checked: no model is configured",
            {"violations": [], "reason": "no_models"},
        )
    settings = await read_guardrail_settings(ctx.db)
    total = len(history.records)

    async def progress(done: int, count: int, _: Any) -> None:
        if done % 200 == 0 or done == count:
            await ctx.stage("guardrails", done, count)

    result = await change.evaluate_history(
        active,
        history.records,
        model_check=None if models is None else rating_check.ModelPolicyCheck(models, context),
        calls_per_record=settings.rating_check_model_calls_per_record,
        progress=progress,
    )
    if result.violations:
        violations = [v.as_json() for v in result.violations]
        raise ImportRefused(
            "rating_check_failed",
            "the story's active history breaks this deployment's guardrails for its rating",
            {"violations": violations},
            errors=[
                {"code": "rating_check_failed", "path": v["position"], **v} for v in violations
            ],
        )
    async with ctx.db.begin() as conn:
        await conn.execute(
            update(operations)
            .where(
                operations.c.story_id == ctx.story_id,
                operations.c.kind == OperationKind.TURN.value,
                operations.c.status == OperationStatus.COMMITTED.value,
                active_operation(operations),
            )
            .values(checked_guardrails=stamp_of(ctx.rating_id, version, found))
        )
    ctx.report["guardrails"] = {"checked": total, "violations": 0}


# -- 3. the outbox -------------------------------------------------------------------------------


async def park_outbox(ctx: ImportContext) -> None:
    from epistrel.core.tables import (
        external_effect_outbox,
        reconciliation_marks,
        state_changes,
        state_definitions,
    )
    from epistrel.core.turns.staged import close_in_fork
    from epistrel.orchestrator.jobs import timers
    from epistrel.orchestrator.jobs.handlers.reservations import RESERVATION_EXPIRY

    reverse = {new: old for old, new in ctx.inserted.ids.items()}
    o = external_effect_outbox.c
    async with ctx.db.begin() as conn:
        closed = await close_in_fork(conn, ctx.story_id, ctx.principal)
        unsettled = [
            str(i)
            for i in await conn.scalars(
                select(o.id).where(o.story_id == ctx.story_id, o.status.in_(UNSETTLED_FROM))
            )
        ]
        if unsettled:
            await conn.execute(
                update(external_effect_outbox)
                .where(o.id.in_(unsettled))
                .values(status="imported_unsettled", executable=False)
            )
            await conn.execute(
                insert(reconciliation_marks),
                [
                    {
                        "id": new_ulid(),
                        "story_id": ctx.story_id,
                        "outbox_id": outbox_id,
                        "reason": "imported_unsettled",
                        "detail": {
                            "source_outbox_id": reverse.get(outbox_id, outbox_id),
                            "source_story_id": ctx.inserted.source_story_id,
                        },
                    }
                    for outbox_id in unsettled
                ],
            )
        c, d = state_changes.c, state_definitions.c
        holds = list(
            await conn.execute(
                select(c.id, d.reservation_expiry_minutes)
                .join(state_definitions, d.id == c.state_id)
                .where(
                    c.story_id == ctx.story_id,
                    c.status == "held",
                    c.membership == Membership.ACTIVE.value,
                    d.reservation_expiry_minutes.is_not(None),
                )
            )
        )
        for hold in holds:
            due = clock.now() + timedelta(minutes=int(hold.reservation_expiry_minutes))
            await timers.schedule(
                conn, RESERVATION_EXPIRY, str(hold.id), due, story_id=ctx.story_id
            )
    ctx.report["outbox"] = {
        "closed_staged_turns": len(closed),
        "parked": len(unsettled),
        "reservation_timers": len(holds),
    }


# -- 4. the profile ------------------------------------------------------------------------------


async def _unregistered_providers(conn: AsyncConnection, story_id: str) -> list[str]:
    """The providers the imported story's external States name that this deployment has not
    registered."""
    from epistrel.core.tables import state_definitions
    from epistrel.core.tables.providers import external_providers

    named = {
        str(p)
        for p in await conn.scalars(
            select(state_definitions.c.provider_id).where(
                state_definitions.c.story_id == story_id,
                state_definitions.c.provider_id.is_not(None),
            )
        )
    }
    if not named:
        return []
    known = {
        str(n)
        for n in await conn.scalars(
            select(external_providers.c.name).where(external_providers.c.name.in_(sorted(named)))
        )
    }
    return sorted(named - known)


async def attach_profile(ctx: ImportContext) -> None:
    from epistrel.core import backfill as core_backfill
    from epistrel.core.backfill import coverage
    from epistrel.core.profiles import effective, schema
    from epistrel.orchestrator.jobs.handlers import backfill as backfill_job

    async with ctx.db.connect() as conn:
        current = await effective.effective_configuration(conn, ctx.story_id, validate=False)
        # FR-PROF-8: what the attach adds over the configuration the bundle ran under (its own
        # profile's settings, with the story's own overrides, which the bundle carries).
        source = dict(ctx.manifest.get("engine_profile") or {})
        same = (str(source.get("id")), int(source.get("version") or 0)) == (
            current.profile_id,
            current.version,
        )
        gained: set[str] = set()
        if not same:
            before = effective.merged_settings(
                current.defaults, dict(source.get("settings") or {}), current.overrides
            )
            gained = set(schema.data_increases(before, current.settings)) - SUMMARY_ONLY
        levels = coverage.capabilities_of(current.settings)
        wanted = {name: levels.get(name) for name in sorted(gained) if name in levels}
        missing = await coverage.gaps_of(conn, ctx.story_id, wanted) if wanted else {}
        unregistered = await _unregistered_providers(conn, ctx.story_id)
    if unregistered:
        # FR-EXT-19: a warning, never a refusal; turns answer ``provider_unregistered`` until
        # the operator registers and binds the provider.
        ctx.report.setdefault("warnings", []).extend(
            {"code": "provider_unregistered", "provider": name} for name in unregistered
        )
    span = core_backfill.span_of(missing)
    mode = ctx.upgrade_mode or "backfill"
    ctx.report["profile"] = {
        "profile_id": ctx.profile_id,
        "gaps": sorted(missing),
        "upgrade_mode": mode if span is not None else None,
    }
    if span is None or mode != "backfill":
        return

    async def start(conn: AsyncConnection) -> None:
        await core_backfill.start(
            conn,
            ctx.story_id,
            kind="gap_fill",
            capabilities=sorted(missing),
            span=span,
            source={
                "profile_id": current.profile_id,
                "profile_version": current.version,
                "overrides_revision": current.overrides_revision,
            },
            target={},
            principal=ctx.principal,
            start_job=backfill_job.start_job,
        )

    ctx.after_ready.append(start)


GATES: Sequence[tuple[str, str, Any]] = (
    ("apply_erasure_ledger", "erasure", apply_erasure_ledger),
    ("check_guardrails", "guardrails", check_guardrails),
    ("park_outbox", "outbox", park_outbox),
    ("attach_profile", "profile", attach_profile),
)
for _name, _stage, _gate in GATES:
    register_gate(_name, _stage, _gate)


__all__ = [
    "GATES",
    "apply_erasure_ledger",
    "attach_profile",
    "check_guardrails",
    "park_outbox",
]
