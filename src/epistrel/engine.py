"""The in-process library: ``epistrel.Engine`` (docs/02 §9, surface 2).

Every REST handler is a thin wrapper that builds a ``RequestContext`` and calls one ``Engine``
method; the library needs no service credential (FR-AUTH-1). The Engine holds its settings and
the database it was bound to (the REST app binds it for the life of the server).
"""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import hashlib
import logging
from collections.abc import Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any

from pydantic import SecretStr, TypeAdapter, ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import ProcessMode, Settings
from epistrel.core import config_changes as config_log
from epistrel.core import (
    deployment,
    fork,
    idempotency,
    ingest,
    operation_view,
    projections,
    ratings,
    stories,
    story_texts,
    timeline,
    turn_gate,
    variants,
)
from epistrel.core import story_settings as settings_store
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.context import RequestContext
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import InvalidRequest, PrincipalRequired
from epistrel.core.history import DEFAULT_LIMIT as DEFAULT_HISTORY_LIMIT
from epistrel.core.history import History, read_history
from epistrel.core.idempotency import (
    DEPLOYMENT_SCOPE_ID,
    IdempotentCall,
    QueuedOriginal,
    Replayed,
    ResultCodec,
)
from epistrel.core.idempotency import Scope as KeyScope
from epistrel.core.ids import new_ulid
from epistrel.core.policies.model import Policy, Scope
from epistrel.core.settings_schema import ConfigClass, StorySettings
from epistrel.core.tables import events
from epistrel.core.turn_gate import Concurrency, QueueItem
from epistrel.core.viewpoint import is_omniscient, resolve_viewpoint, with_requested
from epistrel.orchestrator import queue_drainer
from epistrel.orchestrator.budget import (
    DEFAULT_TURN_DEADLINE_S,
    GUARDRAIL_INPUT,
    GUARDRAIL_OUTPUT,
    TurnBudget,
    effective_max_calls,
)
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.orchestrator.jobs.handlers import turn_queue as turn_queue_job
from epistrel.orchestrator.jobs.handlers.rebuild import (
    REBUILD_KIND,
    RebuildPayload,
    RebuildQueued,
)
from epistrel.orchestrator.jobs.handlers.rebuild import coalesce_key as rebuild_coalesce_key
from epistrel.orchestrator.jobs.worker import (
    Worker,
    WorkerConfig,
    register_recovery_hook,
    unregister_recovery_hook,
)
from epistrel.orchestrator.lite import pipeline as lite
from epistrel.orchestrator.lite.schemas import (
    RegenerateRequest,
    RegenerateResult,
    RespondRequest,
    RespondResult,
)
from epistrel.orchestrator.models import ModelAccessLayer
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.policies import admin as policy_admin
from epistrel.orchestrator.policies import input_gate, output_gate
from epistrel.orchestrator.policies import store as policy_store
from epistrel.orchestrator.policies.age import CastMember
from epistrel.orchestrator.policies.input_gate import EntryPoint, GateText, InputPass
from epistrel.orchestrator.policies.schemas import (
    PolicyFields,
    PolicyReplacement,
    StoryPolicyItem,
)

logger = logging.getLogger(__name__)


def _changed_status(created: int = 200) -> Callable[[Any], int]:
    """200 (or ``created``) for a write that changed something; 204 for one that did not."""
    return lambda write: created if write.changed else 204


_CONFIG = "config_revision"
CODECS: dict[str, ResultCodec[Any]] = {
    "rebuild_projections": ResultCodec.of(
        projections.RebuildResult | RebuildQueued,
        status=lambda result: 202 if isinstance(result, RebuildQueued) else 200,
    ),
    "create_story": ResultCodec.of(stories.CreatedStory, status=201),
    "respond": ResultCodec.of(RespondResult),
    "ingest": ResultCodec.of(
        ingest.IngestResult,
        status=lambda result: 201 if result.created else 200,
        mark=lambda result: dataclasses.replace(result, key_replayed=True),
    ),
    "set_pinned_memories": ResultCodec.of(story_texts.PinnedMemories),
    "set_custom_instructions": ResultCodec.of(story_texts.CustomInstructions),
    "update_story_settings": ResultCodec.of(
        settings_store.StorySettingsWrite, revision_field=_CONFIG
    ),
    "truncate": ResultCodec.of(timeline.Truncation),
    "untruncate": ResultCodec.of(timeline.Untruncation),
    "swap": ResultCodec.of(timeline.Swapped),
    "fork": ResultCodec.of(fork.Forked, status=201),
    "regenerate": ResultCodec.of(RegenerateResult),
    "archive_story": ResultCodec.of(stories.StatusChange),
    "unarchive_story": ResultCodec.of(stories.StatusChange),
    "delete_story": ResultCodec.of(stories.DeletedStory, status=204),
    "create_content_rating": ResultCodec.of(
        ratings.RatingWrite, status=201, revision_field=_CONFIG
    ),
    "update_content_rating": ResultCodec.of(ratings.RatingWrite, revision_field=_CONFIG),
    "retire_content_rating": ResultCodec.of(
        ratings.RatingWrite, status=_changed_status(), revision_field=_CONFIG
    ),
    "update_deployment_settings": ResultCodec.of(
        deployment.DeploymentState, revision_field=_CONFIG
    ),
    "policies_create": ResultCodec.of(policy_admin.PolicyWrite, status=201, revision_field=_CONFIG),
    "policies_update": ResultCodec.of(policy_admin.PolicyWrite, revision_field=_CONFIG),
    "policies_retire": ResultCodec.of(
        policy_admin.PolicyWrite, status=_changed_status(), revision_field=_CONFIG
    ),
    "policies_set_story": ResultCodec.of(policy_admin.StoryPoliciesWrite, revision_field=_CONFIG),
    "replace_model_config": ResultCodec.of(model_config.ConfigWrite, revision_field=_CONFIG),
    "remove_model_override": ResultCodec.of(
        model_config.ConfigWrite, status=_changed_status(), revision_field=_CONFIG
    ),
    "set_model_api_key": ResultCodec.of(
        model_config.ConfigWrite, status=204, revision_field=_CONFIG
    ),
    "remove_model_api_key": ResultCodec.of(
        model_config.ConfigWrite, status=204, revision_field=_CONFIG
    ),
}
"""How each mutating method's result is stored under its ``Idempotency-Key`` and replayed: the
result type, the REST status it answers with, and the revision a reference carries."""

IDEMPOTENCY_WAIT_S = DEFAULT_TURN_DEADLINE_S + idempotency.WAIT_SLACK_S
"""How long a duplicate waits for its original (the turn deadline + 5 s), then 503."""


@dataclasses.dataclass(frozen=True, slots=True)
class EffectiveBudget:
    """A story's effective turn budget (#69; ``GET /stories/{id}/settings``)."""

    max_model_calls_per_turn: int
    source: str
    turn_deadline: float
    reserved: dict[str, int]
    minimum_required: int


def _body(model: Any) -> Any:
    """A validated request body as hashed: its JSON dump with defaults filled."""
    return model.model_dump(mode="json")


_POLICY_ITEMS: TypeAdapter[list[StoryPolicyItem]] = TypeAdapter(list[StoryPolicyItem])


def _policy_items(raw: Any) -> list[StoryPolicyItem]:
    """A settings patch's ``policies`` list (422 ``validation_error`` at its field)."""
    try:
        return _POLICY_ITEMS.validate_python(raw)
    except ValidationError as exc:
        first = exc.errors()[0]
        where = "".join(
            f"[{part}]" if isinstance(part, int) else f".{part}" for part in first["loc"]
        )
        raise InvalidRequest(
            str(first["msg"]), {"field": f"body.policies{where}", "errors": exc.error_count()}
        ) from None


TURN_QUEUE_HOOK = "turn_queue"
"""The worker recovery hook that drains the turn queues a stopped process left (#63, #71)."""


class Engine:
    """The async entry point to Epistrel.

    Every mutating method is idempotent (FR-CONC-6, #61): it runs under the context's
    ``Idempotency-Key`` (a ULID is generated when there is none). A retry with the same key and
    request returns the stored result with ``replayed=True`` within the result window, and
    raises ``idempotency.ReferenceReplay`` after it, never re-executing; the same key with a
    different request is 409 ``idempotency_conflict``; a retry while the original runs waits
    for it. Story calls are keyed per story, creation, deletion and admin calls per deployment.
    """

    def __init__(
        self,
        settings: Settings,
        db: AsyncEngine | None = None,
        models: ModelAccessLayer | None = None,
    ) -> None:
        self.settings = settings
        self._db = db
        # Every model call goes through this one layer (FR-MODEL-1); the adapter is chosen by
        # EPISTREL_MODEL_ADAPTER. Tests reach the stub through ``engine.models.stub``.
        self.models = models or ModelAccessLayer.from_settings(settings)
        self.models.bind(db)
        # Queued turns (FR-CONC-5, #63) are drained in this process: kicked when queued here, and
        # by the worker's ``turn_queue`` recovery hook and ``turn_queue_drain`` task (#71).
        self._recovery: asyncio.Task[None] | None = None
        self.worker: Worker | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self.drainer = queue_drainer.QueueDrainer(
            lambda: self.db,
            {turn_gate.RESPOND: self._drain_respond, turn_gate.INGEST: self._drain_ingest},
        )

    @classmethod
    def from_settings(cls, settings: Settings) -> Engine:
        return cls(settings)

    def bind(self, db: AsyncEngine | None) -> None:
        """Use ``db`` for every operation (``None`` unbinds)."""
        self._db = db
        self.models.bind(db)

    async def start_workers(self, mode: ProcessMode | None = None) -> None:
        """Start the in-process background work (the app's start-up; NFR-REL-5, #71).

        ``all`` and ``worker`` modes run the job worker (``worker_concurrency`` loops) as an
        asyncio task: its recovery pass drains every turn queue a stopped process left behind
        (the ``turn_queue`` recovery hook), and its ``turn_queue_drain`` task re-checks every
        30 s. ``api`` mode runs no worker and only drains the queues left behind once. Either
        way it runs in the background, so an unreachable database never stops the server."""
        mode = mode or self.settings.mode
        turn_queue_job.bind(self.drainer.recover)
        if mode == "api":
            self._recovery = asyncio.create_task(self._recover())
            return
        register_recovery_hook(TURN_QUEUE_HOOK, self._recovery_hook)
        self.worker = Worker(self.db, WorkerConfig.from_settings(self.settings, mode=mode))
        self._worker_task = asyncio.create_task(self.worker.run(), name="epistrel-worker")

    async def _recovery_hook(self, conn: Any) -> None:
        await self.drainer.recover()

    async def _recover(self) -> None:
        try:
            await self.drainer.recover()
        except Exception as exc:
            logger.warning("queued turns were not recovered at start-up: %s", type(exc).__name__)

    async def stop_workers(self) -> None:
        """Stop the background work: the worker lets running jobs finish (up to its
        ``DRAIN_S``, within the lease), then the drainer stops; a queued turn stopped mid-run
        goes back to its queue."""
        recovery, self._recovery = self._recovery, None
        if recovery is not None and not recovery.done():
            recovery.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await recovery
        worker, task = self.worker, self._worker_task
        self.worker, self._worker_task = None, None
        if worker is not None and task is not None:
            worker.stop()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            unregister_recovery_hook(TURN_QUEUE_HOOK)
        if turn_queue_job.bound() == self.drainer.recover:
            turn_queue_job.bind(None)
        await self.drainer.stop()

    @property
    def db(self) -> AsyncEngine:
        if self._db is None:
            raise RuntimeError("the Engine has no database; bind one first")
        return self._db

    async def _keyed[T](
        self,
        ctx: RequestContext,
        operation: str,
        run: Callable[[RequestContext], Awaitable[T]],
        *,
        story_id: str | None = None,
        material: Mapping[str, Any] | None = None,
        respond_async: bool = False,
    ) -> T:
        """Run ``run`` as one idempotent call (see the class docstring).

        ``story_id`` keys the call per story (else per deployment). ``material`` is what the
        request hash covers besides the operation name and the expected revision; ``None``
        means ``run`` claims the key itself at its slot in the precheck order (respond, whose
        hash needs the resolved viewpoint). A retry whose original waits in the story's turn
        queue waits for that turn (#63), like the original: up to ``turn_deadline``, or not at
        all with ``respond_async`` (then ``TurnAccepted``, REST 202)."""
        principal = self.require_principal(ctx)
        key = ctx.idempotency_key
        key = new_ulid() if key is None else idempotency.validate_key(key)
        ctx = dataclasses.replace(ctx, idempotency_key=key)
        codec: ResultCodec[T] = CODECS[operation]
        call = IdempotentCall(
            self.db,
            scope=KeyScope.STORY if story_id is not None else KeyScope.DEPLOYMENT,
            scope_id=story_id if story_id is not None else DEPLOYMENT_SCOPE_ID,
            key=key,
            operation=operation,
            principal=principal,
            codec=codec,
            wait_s=IDEMPOTENCY_WAIT_S,
        )
        with idempotency.calling(call):
            try:
                if material is not None:
                    await call.claim({**material, "expected_revision": ctx.expected_revision})
                value = await run(ctx)
            except Replayed as replay:
                return codec.load(replay.stored)
            except QueuedOriginal as queued:
                assert story_id is not None  # noqa: S101 - only story calls queue
                found: T = await self._await_queued(
                    story_id,
                    queued.operation_id,
                    operation,
                    key,
                    respond_async=respond_async,
                    replayed=True,
                )
                return found
            except BaseException:
                await call.release()
                raise
            finished: T = await call.finish(value)
            return finished

    @staticmethod
    def require_principal(ctx: RequestContext) -> str:
        """Every mutation carries a requesting principal (FR-AUTH-4); the library checks too."""
        if ctx.principal is None or not ctx.principal.strip():
            raise PrincipalRequired("a mutating call needs a requesting principal")
        return ctx.principal

    # -- the turn queue (FR-CONC-5; #63) ---------------------------------------------------------

    async def _queue[T](
        self,
        ctx: RequestContext,
        story_id: str,
        *,
        kind: str,
        operation: str,
        operation_id: str,
        request: dict[str, Any],
        viewpoint: str | None,
        origin: str,
        concurrency: Concurrency,
        respond_async: bool,
    ) -> T:
        """Queue a turn that found its story busy under ``queue``, hand it the request's
        ``Idempotency-Key``, start the drainer, and wait for the turn (long-poll up to
        ``turn_deadline``; ``respond_async``: not at all) — then its result, its error, or 202
        (``TurnAccepted``) with the turn still queued."""
        principal = self.require_principal(ctx)
        call = idempotency.current_call()
        await turn_gate.enqueue(
            self.db,
            story_id,
            operation_id=operation_id,
            kind=kind,
            request=request,
            principal=principal,
            idempotency_key=ctx.idempotency_key,
            viewpoint=viewpoint,
            origin=origin,
            queue_max=concurrency.queue_max,
            hand_over=call.hand_over if call is not None else None,
        )
        self.drainer.kick(story_id)
        assert ctx.idempotency_key is not None  # noqa: S101 - every call is keyed (#61)
        found: T = await self._await_queued(
            story_id,
            operation_id,
            operation,
            ctx.idempotency_key,
            respond_async=respond_async,
            replayed=False,
            concurrency=concurrency,
        )
        return found

    async def _await_queued(
        self,
        story_id: str,
        operation_id: str,
        operation: str,
        key: str,
        *,
        respond_async: bool,
        replayed: bool,
        concurrency: Concurrency | None = None,
    ) -> Any:
        codec = CODECS[operation]
        if concurrency is None:
            concurrency = await turn_gate.read_concurrency(self.db, story_id)
        wait_s = 0.0 if respond_async else concurrency.turn_deadline
        outcome = await queue_drainer.await_turn(
            self.db, story_id, operation_id, wait_s=wait_s, key=key
        )
        stored = await queue_drainer.answer(
            self.db,
            story_id,
            operation_id,
            outcome,
            key=key,
            revision_field=codec.revision_field,
        )
        return codec.load(stored) if replayed else codec.adapter.validate_python(stored.body)

    async def _as_queued(
        self, item: QueueItem, operation: str, run: Callable[[], Awaitable[Any]]
    ) -> None:
        """Run a queued turn under the ``Idempotency-Key`` its request handed to the queue:
        the turn's commit binds it and its result is stored there for the waiting requests."""
        if item.idempotency_key is None:  # pragma: no cover - every call is keyed (#61)
            await run()
            return
        call = IdempotentCall.owned_by(
            self.db,
            turn_gate.queue_owner(item.operation_id),
            scope=KeyScope.STORY,
            scope_id=item.story_id,
            key=item.idempotency_key,
            operation=operation,
            principal=item.principal,
            codec=CODECS[operation],
        )
        await call.load_window()
        with idempotency.calling(call):
            try:
                value = await run()
            except asyncio.CancelledError:
                raise  # the turn goes back to the queue with its key
            except BaseException:
                await call.release()
                raise
            await call.finish(value)

    async def _drain_respond(self, item: QueueItem, concurrency: Concurrency) -> None:
        request = RespondRequest.model_validate(item.request["body"])
        ctx = RequestContext(
            principal=item.principal,
            idempotency_key=item.idempotency_key,
            viewpoint=item.request.get("viewpoint"),
        )
        await self._as_queued(
            item,
            "respond",
            lambda: lite.run_queued(
                self.db,
                self.models,
                ctx,
                item.story_id,
                request,
                operation_id=item.operation_id,
                principal=item.principal,
                concurrency=concurrency,
                planned=item.request.get("turn_budget"),
            ),
        )

    async def _drain_ingest(self, item: QueueItem, concurrency: Concurrency) -> None:
        request = ingest.IngestRequest.model_validate(item.request["body"])
        await self._as_queued(
            item,
            "ingest",
            lambda: ingest.run_queued(
                self.db,
                item.story_id,
                request,
                principal=item.principal,
                idempotency_key=item.idempotency_key,
                operation_id=item.operation_id,
            ),
        )

    async def noop(self, ctx: RequestContext) -> str:
        """A mutating call that changes nothing and returns the recorded principal.

        Stands in for real operations (#45 onward) so the trust boundary can be proven before
        any exists; it is reachable only from the library and test-registered routes.
        """
        return self.require_principal(ctx)

    async def rebuild_projections(
        self, ctx: RequestContext, story_id: str
    ) -> projections.RebuildResult | RebuildQueued:
        """Refold every projection of ``story_id`` from the event log (FR-ADMIN-3).

        A story with at most ``settings.rebuild_async_threshold`` events is rebuilt in the call;
        a larger one is handed to a worker as a ``rebuild_projections`` job (``RebuildQueued``,
        REST 202 ``{job_id, timeline_revision}``; a rebuild already queued for the story is
        that job, #70).
        """
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> projections.RebuildResult | RebuildQueued:
            revision = await self.check_expected_revision(ctx, story_id)
            async with self.db.begin() as conn:
                count = await conn.scalar(
                    select(func.count()).select_from(events).where(events.c.story_id == story_id)
                )
                if int(count or 0) > self.settings.rebuild_async_threshold:
                    job_id = await job_queue.enqueue(
                        conn,
                        REBUILD_KIND,
                        RebuildPayload(story_id=story_id),
                        story_id=story_id,
                        coalesce_key=rebuild_coalesce_key(story_id),
                    )
                    return RebuildQueued(job_id=job_id, timeline_revision=revision)
            return await projections.rebuild(self.db, story_id, principal=principal)

        return await self._keyed(
            ctx, "rebuild_projections", run, story_id=story_id, material={"story_id": story_id}
        )

    async def check_expected_revision(self, ctx: RequestContext, story_id: str) -> int:
        """Every story-scoped mutation starts here: a stale ``ctx.expected_revision`` is 409
        ``revision_conflict`` with ``current_revision`` before anything is written (FR-RESP-6).

        A start-time precondition only; the commit itself revalidates under FR-CONC-4.
        Deployment-scoped calls never call it, so the header is ignored there.
        """
        return await check_expected_revision(self.db, story_id, ctx.expected_revision)

    async def history(
        self,
        ctx: RequestContext,
        story_id: str,
        *,
        viewpoint: str | None = None,
        actor_character_id: str | None = None,
        as_of: int | None = None,
        limit: int = DEFAULT_HISTORY_LIMIT,
        before_turn: int | None = None,
        include_audit: bool = False,
        variant: variants.ChainRef | None = None,
    ) -> History:
        """The story's corrected history through ``as_of`` (default the head), produced for one
        output viewpoint (FR-RESP-12): ``viewpoint`` (or ``ctx.viewpoint``) when named, else
        ``actor_character_id``, else the story's default; 422 ``viewpoint_unresolved`` when none
        resolves. A character viewpoint never gets ``think``/``latent`` beats; audit records
        (``include_audit``) need ``omniscient`` by name (403 ``omniscient_required``). Pages of
        whole turns, newest first, up to ``limit`` items; ``before_turn`` pages backwards.
        ``variant`` reads along that chain instead of the active one (#66)."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        return await read_history(
            self.db,
            story_id,
            viewpoint=ctx.viewpoint,
            actor_character_id=actor_character_id,
            as_of=as_of,
            limit=limit,
            before_turn=before_turn,
            include_audit=include_audit,
            variant=variant,
        )

    async def get_operation(
        self, ctx: RequestContext, operation_id: str, *, viewpoint: str | None = None
    ) -> operation_view.OperationView:
        """One operation through the history API (FR-CONC-6): kind, status, position, the
        story's revision once it committed, attempts, origin and, within the result window,
        its stored result. A result made for a viewpoint is returned only to a read naming
        that viewpoint (``viewpoint`` or ``ctx.viewpoint``); another named viewpoint is 409
        ``idempotency_conflict``. Unknown is 404."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        return await operation_view.read_operation(self.db, operation_id, viewpoint=ctx.viewpoint)

    # -- story lifecycle (FR-ADMIN-1, FR-STORE-14,16, FR-CAT-12) ----------------------------

    async def create_story(
        self, ctx: RequestContext, spec: stories.StorySpec
    ) -> stories.CreatedStory:
        """Create a blank story with its cast, rating and settings in one genesis operation.

        Every character ``definition`` goes through the input gate first (FR-GUARD-10), against
        the global policies and the chosen rating's, with the cast's true ages: a violation is
        422 ``guardrail_violation`` and no story exists."""
        principal = self.require_principal(ctx)
        stories.validate_spec(spec)
        return await self._keyed(
            ctx,
            "create_story",
            lambda ctx: self._create_story(ctx, spec, principal),
            material={"body": _body(spec)},
        )

    async def _create_story(
        self, ctx: RequestContext, spec: stories.StorySpec, principal: str
    ) -> stories.CreatedStory:
        async with self.db.connect() as conn, conn.begin():
            rating_id = await stories.resolve_story_rating(conn, spec.content_rating_id)
        cast = [
            CastMember(character_id=f"new:{index}", ordinal=index, true_age=c.true_age)
            for index, c in enumerate(spec.characters)
        ]
        context = await policy_store.creation_context(self.db, rating_id, cast)
        texts = [
            GateText(f"characters[{index}].definition", c.definition)
            for index, c in enumerate(spec.characters)
        ]
        await input_gate.enforce_input(
            self.db,
            self.models,
            context,
            texts,
            entry_point=EntryPoint.STORY_CREATE,
            principal=principal,
            story_id=None,
        )
        return await stories.create_story_from(self.db, spec, principal=principal)

    async def gate_input(
        self,
        ctx: RequestContext,
        story_id: str,
        entry_point: EntryPoint,
        texts: Sequence[GateText],
        *,
        actor: str | None = None,
        budget: TurnBudget | None = None,
        reserved: int = 0,
        reserve_output_for: Sequence[str] | None = None,
    ) -> InputPass:
        """The input gate of a truth-changing path that accepts text (FR-GUARD-9,10): run it
        after the call's other prechecks (#44's order) and before its ``Operation`` opens. A
        violation writes the ``audit_log`` row and raises 422 ``guardrail_violation``; nothing
        else is written. Respond (#57), ingest (#58) and pins/instructions (#60) call it.

        ``reserve_output_for`` (a turn's render characters) adds the turn's first output check
        to the reservation: one call per model-assisted output policy (#68)."""
        principal = self.require_principal(ctx)
        context = await policy_store.guard_context(self.db, story_id)
        if reserve_output_for is not None:
            reserved += output_gate.output_reservation(context, reserve_output_for)
        deadline_s = DEFAULT_TURN_DEADLINE_S
        if budget is None:
            # A gate outside a turn runs under the story's ``turn_deadline`` (#69).
            deadline_s = (await turn_gate.read_concurrency(self.db, story_id)).turn_deadline
        return await input_gate.enforce_input(
            self.db,
            self.models,
            context,
            texts,
            entry_point=entry_point,
            principal=principal,
            story_id=story_id,
            budget=budget,
            actor=actor,
            reserved=reserved,
            deadline_s=deadline_s,
        )

    async def respond(
        self,
        ctx: RequestContext,
        story_id: str,
        request: RespondRequest,
        *,
        output_viewpoint: str | None = None,
        respond_async: bool = False,
    ) -> RespondResult:
        """Play one Lite turn: the actor's input in, one finished reply out (FR-RESP-1).

        The consumer does no retrieval or memory work (FR-RESP-2): the prompt is assembled from
        the story's own records, never from ``scene_context`` (FR-RESP-4). The input is the
        turn's first beat (FR-RESP-9) and goes through the input gate before anything is
        recorded; the turn makes one ``generation`` call (FR-LITE-7). The message is
        *finished*: under ``lite`` none of its claims is checked.

        The output viewpoint (FR-RESP-12) is ``output_viewpoint``, the request's
        ``output_viewpoint`` or ``ctx.viewpoint`` (one value; two different ones are 422), else
        the actor; the result echoes it and the turn records it.

        Turn concurrency (FR-CONC-5, #63): while another turn of the story runs, a story under
        ``reject`` answers 423 ``story_busy``; one under ``queue`` queues the turn and waits
        for it up to ``turn_deadline`` — with ``respond_async`` it does not wait — and then
        raises ``turn_gate.TurnAccepted`` (REST 202) with the turn still queued. An idle story
        always plays the turn at once."""
        principal = self.require_principal(ctx)
        ctx = with_requested(ctx, output_viewpoint, request.output_viewpoint)

        async def gate(budget: TurnBudget, text: str, actor: str, renders: list[str]) -> InputPass:
            return await self.gate_input(
                ctx,
                story_id,
                EntryPoint.RESPOND,
                [GateText(lite.INPUT_LOCATOR, text)],
                actor=actor,
                budget=budget,
                reserved=1,
                reserve_output_for=renders,
            )

        async def run(ctx: RequestContext) -> RespondResult:
            async def queue_turn(
                operation_id: str,
                queued: dict[str, Any],
                viewpoint: str,
                concurrency: Concurrency,
            ) -> RespondResult:
                return await self._queue(
                    ctx,
                    story_id,
                    kind=turn_gate.RESPOND,
                    operation="respond",
                    operation_id=operation_id,
                    request={**queued, "viewpoint": viewpoint},
                    viewpoint=viewpoint,
                    origin="generated",
                    concurrency=concurrency,
                    respond_async=respond_async,
                )

            return await lite.respond(
                self.db,
                self.models,
                ctx,
                story_id,
                request,
                principal=principal,
                gate=gate,
                queue_turn=queue_turn,
            )

        # Respond claims its key itself, after the cast and archived checks: the hash covers
        # the resolved viewpoint (FR-RESP-12; #59's hand-off).
        return await self._keyed(
            ctx, "respond", run, story_id=story_id, respond_async=respond_async
        )

    async def ingest(
        self,
        ctx: RequestContext,
        story_id: str,
        request: ingest.IngestRequest,
        *,
        respond_async: bool = False,
    ) -> ingest.IngestResult:
        """Record consumer events into authoritative history without generating (FR-RESP-10).

        Each event is its own ``turn`` at the next index (revision +1), all or nothing; a
        ``client_event_id`` the story has seen returns the original result (same payload) or
        409 ``client_event_conflict`` (different payload), writing nothing. Every new event's
        text passes the input gate on its own before anything is recorded. Under turn
        concurrency (#63) the batch is one turn: refused while a generated turn runs under
        ``reject``, queued as one ``turn_queue`` row under ``queue`` (as ``respond``)."""
        principal = self.require_principal(ctx)

        async def gate(groups: Sequence[ingest.GateGroup]) -> None:
            for group in groups:
                await self.gate_input(
                    ctx,
                    story_id,
                    EntryPoint.INGEST,
                    [GateText(locator, text) for locator, text in group.texts],
                    actor=group.actor,
                )

        async def run(ctx: RequestContext) -> ingest.IngestResult:
            async def queue_batch(
                operation_id: str, queued: dict[str, Any], concurrency: Concurrency
            ) -> ingest.IngestResult:
                return await self._queue(
                    ctx,
                    story_id,
                    kind=turn_gate.INGEST,
                    operation="ingest",
                    operation_id=operation_id,
                    request=queued,
                    viewpoint=None,
                    origin=ingest.INGESTED,
                    concurrency=concurrency,
                    respond_async=respond_async,
                )

            return await ingest.ingest(
                self.db,
                story_id,
                request,
                principal=principal,
                gate=gate,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
                queue_batch=queue_batch,
            )

        return await self._keyed(
            ctx,
            "ingest",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
            respond_async=respond_async,
        )

    # -- story-level text: pinned memories and custom instructions (FR-LITE-3) ---------------

    async def pinned_memories(
        self, ctx: RequestContext, story_id: str
    ) -> story_texts.PinnedMemories:
        """The story's pinned memories in order, with its revision (viewpoint-free)."""
        return await story_texts.read_pinned_memories(self.db, story_id)

    async def set_pinned_memories(
        self, ctx: RequestContext, story_id: str, request: story_texts.PinnedMemoriesRequest
    ) -> story_texts.PinnedMemories:
        """Replace the story's pinned memories: every item passes the input gate first; the
        edit bumps the revision (FR-CONC-1), so a turn generating meanwhile regenerates before
        it commits. An edit that changes nothing writes nothing."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> story_texts.PinnedMemories:
            return await story_texts.set_pinned_memories(
                self.db,
                story_id,
                request,
                principal=principal,
                gate=self._text_gate(ctx, story_id, EntryPoint.PINNED_MEMORIES),
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "set_pinned_memories",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    async def custom_instructions(
        self, ctx: RequestContext, story_id: str
    ) -> story_texts.CustomInstructions:
        """The story's custom instructions (``""`` when never set), with its revision."""
        return await story_texts.read_custom_instructions(self.db, story_id)

    async def set_custom_instructions(
        self, ctx: RequestContext, story_id: str, request: story_texts.CustomInstructionsRequest
    ) -> story_texts.CustomInstructions:
        """Replace the story's custom instructions (``""`` clears them), under the same rules
        as ``set_pinned_memories``. Instructions are guidance, never facts."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> story_texts.CustomInstructions:
            return await story_texts.set_custom_instructions(
                self.db,
                story_id,
                request,
                principal=principal,
                gate=self._text_gate(ctx, story_id, EntryPoint.CUSTOM_INSTRUCTIONS),
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "set_custom_instructions",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    def _text_gate(
        self, ctx: RequestContext, story_id: str, entry_point: EntryPoint
    ) -> story_texts.Gate:
        async def gate(texts: Sequence[tuple[str, str]]) -> None:
            await self.gate_input(
                ctx, story_id, entry_point, [GateText(locator, text) for locator, text in texts]
            )

        return gate

    async def story_settings(
        self, ctx: RequestContext, story_id: str
    ) -> tuple[StorySettings, policy_store.StoryGuardrails]:
        """The story's settings (defaults filled in) and its guardrails: effective policies
        with their kind and source, inactive ones, and the model calls they add (FR-GUARD-9)."""
        story = await stories.get_story(self.db, story_id)
        guardrails = await policy_store.story_guardrails(self.db, story_id)
        return StorySettings.from_row(story.settings), guardrails

    async def effective_budget(self, ctx: RequestContext, story_id: str) -> EffectiveBudget:
        """The turn budget the story's next turn would plan with (#69): the call cap and where
        it comes from (``story``, ``deployment`` or ``formula``), ``turn_deadline``, the
        guardrail calls G it reserves (the input check of a turn whose actor has no
        character-scoped input policy) and the safety minimum ``1 + G``."""
        concurrency = await turn_gate.read_concurrency(self.db, story_id)
        guardrails = await policy_store.story_guardrails(self.db, story_id)
        reserved = {
            GUARDRAIL_INPUT: guardrails.calls_input,
            GUARDRAIL_OUTPUT: guardrails.calls_output,
        }
        cap, source = effective_max_calls(
            concurrency.max_model_calls_per_turn,
            concurrency.deployment_max_calls,
            sum(reserved.values()),
        )
        return EffectiveBudget(
            max_model_calls_per_turn=cap,
            source=source,
            turn_deadline=concurrency.turn_deadline,
            reserved=reserved,
            minimum_required=1 + sum(reserved.values()),
        )

    async def update_story_settings(
        self, ctx: RequestContext, story_id: str, patch: Mapping[str, Any]
    ) -> settings_store.StorySettingsWrite:
        """Change the story's settings (``PATCH /stories/{id}/settings``, FR-CONC-5,15): a JSON
        merge patch over ``StorySettings`` (``null`` resets a setting to its default; an
        unknown key is 422; the default viewpoint must be a cast member, never
        ``omniscient``), plus an optional ``policies`` list that upserts the story's own
        policies (refused whole with 422 ``cannot_relax`` before anything is written if any
        item relaxes). Prospective: the next request runs under the new settings, a turn
        already running keeps its own. Each write moves the story's ``config_revision``."""
        principal = self.require_principal(ctx)
        body = dict(patch)
        items: list[StoryPolicyItem] | None = None
        if "policies" in body:
            items = _policy_items(body.pop("policies"))
            for index, item in enumerate(items):
                policy_admin.refuse_relaxing(item.effect, field=f"body.policies[{index}].effect")
        settings_store.check_keys(body)

        async def run(ctx: RequestContext) -> settings_store.StorySettingsWrite:
            await self.check_expected_revision(ctx, story_id)
            write = await settings_store.update_story_settings(
                self.db, story_id, body, principal=principal
            )
            if items is not None:
                policies = await policy_admin.set_story_policies(
                    self.db, story_id, items, principal=principal
                )
                write = dataclasses.replace(
                    write,
                    config_revision=max(write.config_revision, policies.config_revision),
                    changed=write.changed or policies.changed,
                )
            return write

        material: dict[str, Any] = {"story_id": story_id, "body": body}
        if items is not None:
            material["policies"] = [_body(item) for item in items]
        return await self._keyed(
            ctx, "update_story_settings", run, story_id=story_id, material=material
        )

    async def get_story(
        self, ctx: RequestContext, story_id: str, *, viewpoint: str | None = None
    ) -> stories.StoryView:
        """The story and its cast. True ages only when the read names ``omniscient``
        (FR-CAT-12); a named viewpoint that is not ``omniscient`` or ``character:<id>`` of the
        cast is 422 ``viewpoint_unresolved``. With none named the read is character-safe."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        story = await stories.get_story(self.db, story_id, omniscient=False)
        if ctx.viewpoint is None:
            return story
        resolved = resolve_viewpoint(
            ctx.viewpoint,
            actor_character_id=None,
            story_default=None,
            cast_ids={c.id for c in story.characters},
            field="query.viewpoint",
        )
        if not is_omniscient(resolved):
            return story
        return await stories.get_story(self.db, story_id, omniscient=True)

    async def undo_state(self, ctx: RequestContext, story_id: str) -> timeline.UndoState:
        """Whether the story's latest truncation can be undone, and the discarded tails that
        have newer turns over them (``GET /stories/{id}``, #65)."""
        return await timeline.undo_state(self.db, story_id)

    async def truncate(
        self, ctx: RequestContext, story_id: str, request: timeline.TruncateRequest
    ) -> timeline.Truncation:
        """Discard everything positioned after ``request.to_turn`` (FR-STORE-10): membership
        only, nothing deleted; a turn generating meanwhile abandons with ``timeline_changed``
        and queued turns fail with it."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> timeline.Truncation:
            return await timeline.truncate(
                self.db,
                story_id,
                request,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "truncate",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    async def untruncate(
        self,
        ctx: RequestContext,
        story_id: str,
        request: timeline.UntruncateRequest | None = None,
    ) -> timeline.Untruncation:
        """Revert the most recent truncation (FR-STORE-18, LIFO): 409 ``nothing_to_undo``
        without one; a swap (#66) when newer turns were written over it."""
        principal = self.require_principal(ctx)
        request = request or timeline.UntruncateRequest()

        async def run(ctx: RequestContext) -> timeline.Untruncation:
            return await timeline.untruncate(
                self.db,
                story_id,
                request,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "untruncate",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    async def swap(
        self, ctx: RequestContext, story_id: str, request: variants.SwapRequest
    ) -> timeline.Swapped:
        """Make the chain ``request.to_variant_chain`` active and the current continuation
        dormant (FR-STORE-18,28; #66): membership only, reversible by swapping back. Swapping to
        the active chain writes nothing; 409 ``unknown_chain`` for a chain that is not one."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> timeline.Swapped:
            return await timeline.swap(
                self.db,
                story_id,
                request,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "swap",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    async def fork(
        self, ctx: RequestContext, story_id: str, request: fork.ForkRequest
    ) -> fork.Forked:
        """Copy the story's corrected history through ``request.at_turn`` (along
        ``request.variant_chain``) into a new, independent story (FR-STORE-15, FR-CONC-10; #67).
        Keyed per deployment, like story creation; the expected revision is the source's."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> fork.Forked:
            return await fork.fork(
                self.db,
                story_id,
                request,
                principal=principal,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx, "fork", run, material={"story_id": story_id, "body": _body(request)}
        )

    async def variants(self, ctx: RequestContext, story_id: str) -> list[variants.Chain]:
        """The story's variant chains (``GET /stories/{id}/variants``, #66)."""
        return await variants.list_chains(self.db, story_id)

    async def regenerate(
        self,
        ctx: RequestContext,
        story_id: str,
        request: RegenerateRequest | None = None,
        *,
        output_viewpoint: str | None = None,
    ) -> RegenerateResult:
        """Play the latest turn again as a new variant from its recorded input (FR-STORE-12,
        #66); the old variant goes dormant in the same commit and can be swapped back. The
        input is re-gated (#56); a busy story is 423 ``story_busy``; queued turns fail with
        ``timeline_changed``. Omitted overrides reuse the recorded render set, viewpoint and
        generation settings."""
        principal = self.require_principal(ctx)
        request = request or RegenerateRequest()
        ctx = with_requested(ctx, output_viewpoint, request.output_viewpoint)

        async def gate(budget: TurnBudget, text: str, actor: str, renders: list[str]) -> InputPass:
            return await self.gate_input(
                ctx,
                story_id,
                EntryPoint.REGENERATE,
                [GateText(lite.INPUT_LOCATOR, text)],
                actor=actor,
                budget=budget,
                reserved=1,
                reserve_output_for=renders,
            )

        async def run(ctx: RequestContext) -> RegenerateResult:
            return await lite.regenerate(
                self.db, self.models, ctx, story_id, request, principal=principal, gate=gate
            )

        return await self._keyed(
            ctx,
            "regenerate",
            run,
            story_id=story_id,
            material={
                "story_id": story_id,
                "body": _body(request),
                "viewpoint": ctx.viewpoint,
            },
        )

    async def list_stories(
        self, ctx: RequestContext, *, status: StoryStatus | None = None
    ) -> list[stories.StoryView]:
        return await stories.list_stories(self.db, status=status)

    async def archive_story(self, ctx: RequestContext, story_id: str) -> stories.StatusChange:
        """Make the story read-only; idempotent; 423 while a turn is in flight."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> stories.StatusChange:
            await self.check_expected_revision(ctx, story_id)
            return await stories.archive_story(self.db, story_id, principal=principal)

        return await self._keyed(
            ctx, "archive_story", run, story_id=story_id, material={"story_id": story_id}
        )

    async def unarchive_story(self, ctx: RequestContext, story_id: str) -> stories.StatusChange:
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> stories.StatusChange:
            await self.check_expected_revision(ctx, story_id)
            return await stories.unarchive_story(self.db, story_id, principal=principal)

        return await self._keyed(
            ctx, "unarchive_story", run, story_id=story_id, material={"story_id": story_id}
        )

    async def delete_story(self, ctx: RequestContext, story_id: str) -> stories.DeletedStory:
        """Remove every row of the story and nothing else; returns rows removed per table.

        Keyed per deployment (the story's own keys go with it)."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> stories.DeletedStory:
            await self.check_expected_revision(ctx, story_id)
            removed = await stories.delete_story(self.db, story_id, principal=principal)
            return stories.DeletedStory(removed)

        return await self._keyed(ctx, "delete_story", run, material={"story_id": story_id})

    # -- content ratings and deployment settings (FR-RATE-1,2; deployment-scoped) --------------

    async def list_content_ratings(
        self, ctx: RequestContext, *, include_retired: bool = False
    ) -> list[ratings.Rating]:
        return await ratings.list_ratings(self.db, include_retired=include_retired)

    async def get_content_rating(self, ctx: RequestContext, rating_id: str) -> ratings.RatingDetail:
        return await ratings.get_rating(self.db, rating_id)

    async def content_rating_versions(
        self, ctx: RequestContext, rating_id: str
    ) -> list[ratings.Rating]:
        return await ratings.rating_versions(self.db, rating_id)

    async def create_content_rating(
        self, ctx: RequestContext, fields: ratings.RatingFields
    ) -> ratings.RatingWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "create_content_rating",
            lambda _: ratings.create_rating(self.db, fields, principal=principal),
            material={"body": _body(fields)},
        )

    async def update_content_rating(
        self, ctx: RequestContext, rating_id: str, fields: ratings.RatingReplacement
    ) -> ratings.RatingWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "update_content_rating",
            lambda _: ratings.update_rating(self.db, rating_id, fields, principal=principal),
            material={"rating_id": rating_id, "body": _body(fields)},
        )

    async def retire_content_rating(
        self, ctx: RequestContext, rating_id: str
    ) -> ratings.RatingWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "retire_content_rating",
            lambda _: ratings.retire_rating(self.db, rating_id, principal=principal),
            material={"rating_id": rating_id},
        )

    async def deployment_settings(self, ctx: RequestContext) -> deployment.DeploymentState:
        return await deployment.read_settings(self.db)

    async def update_deployment_settings(
        self, ctx: RequestContext, patch: deployment.SettingsPatch
    ) -> deployment.DeploymentState:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "update_deployment_settings",
            lambda _: deployment.update_settings(self.db, patch, principal=principal),
            material={"body": patch.model_dump(mode="json", exclude_unset=True)},
        )

    async def config_changes(
        self,
        ctx: RequestContext,
        *,
        scope: config_log.ConfigScope,
        story_id: str | None = None,
        settings: Sequence[str] = (),
        change_class: ConfigClass | None = None,
        since: datetime | None = None,
        after: str | None = None,
        limit: int = config_log.DEFAULT_PAGE,
    ) -> config_log.ConfigChangePage:
        """A page of the configuration log (FR-CONC-14, #64): the deployment's changes, or a
        story's (404 for an unknown story), newest first."""
        if story_id is not None:
            await check_expected_revision(self.db, story_id, None)
        return await config_log.list_config_changes(
            self.db,
            scope=scope,
            scope_id=story_id,
            settings=settings,
            change_class=change_class,
            since=since,
            after=after,
            limit=limit,
        )

    # -- the policy store (FR-GUARD-1,8,9; NFR-SEC-1) ----------------------------------------
    # The writes below are the only callers of ``orchestrator.policies.admin``: the operator
    # surface (``/admin/policies``) and story settings (#63). Nothing on a turn's path calls them.

    async def policies_list(
        self,
        ctx: RequestContext,
        *,
        scope: Scope | None = None,
        story_id: str | None = None,
        include_retired: bool = False,
    ) -> list[Policy]:
        return await policy_store.list_policies(
            self.db, scope=scope, story_id=story_id, include_retired=include_retired
        )

    async def policies_get(self, ctx: RequestContext, policy_id: str) -> Policy:
        return await policy_store.get_policy(self.db, policy_id)

    async def policies_effective(self, ctx: RequestContext, story_id: str) -> tuple[Policy, ...]:
        """The story's effective policies: global, the rating closure, story, character."""
        return await policy_store.effective_policies(self.db, story_id)

    async def policies_create(
        self, ctx: RequestContext, fields: PolicyFields
    ) -> policy_admin.PolicyWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "policies_create",
            lambda ctx: policy_admin.create_policy(
                self.db, fields, principal=principal, idempotency_key=ctx.idempotency_key
            ),
            material={"body": _body(fields)},
        )

    async def policies_update(
        self, ctx: RequestContext, policy_id: str, fields: PolicyReplacement
    ) -> policy_admin.PolicyWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "policies_update",
            lambda _: policy_admin.update_policy(self.db, policy_id, fields, principal=principal),
            material={"policy_id": policy_id, "body": _body(fields)},
        )

    async def policies_retire(
        self, ctx: RequestContext, policy_id: str
    ) -> policy_admin.PolicyWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "policies_retire",
            lambda _: policy_admin.retire_policy(self.db, policy_id, principal=principal),
            material={"policy_id": policy_id},
        )

    async def policies_set_story(
        self, ctx: RequestContext, story_id: str, items: list[StoryPolicyItem]
    ) -> policy_admin.StoryPoliciesWrite:
        """Upsert the story's own policies (#63's ``PATCH /stories/{id}/settings``)."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> policy_admin.StoryPoliciesWrite:
            await self.check_expected_revision(ctx, story_id)
            return await policy_admin.set_story_policies(
                self.db, story_id, items, principal=principal
            )

        return await self._keyed(
            ctx,
            "policies_set_story",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": [_body(item) for item in items]},
        )

    # -- model configuration (FR-MODEL-1..4; NFR-SEC-3) ---------------------------------------
    # Deployment-scoped: every write bumps ``config_revision`` and records a prospective
    # ``config_changes`` row; the next model call reads the new configuration (no restart).

    async def model_configuration(
        self, ctx: RequestContext
    ) -> tuple[int, dict[str, model_config.ConfigEntry]]:
        """``config_revision`` and every configurable purpose, ``default`` included."""
        return await self.models.configuration()

    async def model_config_entry(
        self, ctx: RequestContext, purpose: str
    ) -> tuple[int, model_config.ConfigEntry]:
        return await model_config.entry(self.db, self.models.keybox, purpose)

    async def replace_model_config(
        self, ctx: RequestContext, purpose: str, body: model_config.ConfigReplacement
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "replace_model_config",
            lambda _: model_config.replace_config(
                self.db, purpose, body, keybox=self.models.keybox, principal=principal
            ),
            material={"purpose": purpose, "body": _body(body)},
        )

    async def remove_model_override(
        self, ctx: RequestContext, purpose: str
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "remove_model_override",
            lambda _: model_config.remove_override(
                self.db, purpose, keybox=self.models.keybox, principal=principal
            ),
            material={"purpose": purpose},
        )

    async def set_model_api_key(
        self, ctx: RequestContext, purpose: str, api_key: SecretStr
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        # The hash covers the key's digest, never the key (NFR-SEC-3).
        digest = hashlib.sha256(api_key.get_secret_value().encode()).hexdigest()
        return await self._keyed(
            ctx,
            "set_model_api_key",
            lambda _: model_config.set_api_key(
                self.db, purpose, api_key, keybox=self.models.keybox, principal=principal
            ),
            material={"purpose": purpose, "api_key_sha256": digest},
        )

    async def remove_model_api_key(
        self, ctx: RequestContext, purpose: str
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "remove_model_api_key",
            lambda _: model_config.remove_api_key(
                self.db, purpose, keybox=self.models.keybox, principal=principal
            ),
            material={"purpose": purpose},
        )
