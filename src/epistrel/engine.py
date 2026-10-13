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
import inspect
import logging
import time
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol

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
    ownership,
    projections,
    ratings,
    stories,
    story_texts,
    timeline,
    turn_gate,
    variants,
)
from epistrel.core import story_settings as settings_store
from epistrel.core.catalog import characters as catalog_characters
from epistrel.core.catalog import kinds as catalog_kinds
from epistrel.core.catalog import listing as catalog_listing
from epistrel.core.catalog import scenario_models
from epistrel.core.catalog import scenarios as catalog_scenarios
from epistrel.core.catalog import search as catalog_search
from epistrel.core.catalog.report import ValidationResult
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.context import RequestContext
from epistrel.core.enums import EntryStatus, StoryStatus
from epistrel.core.errors import InvalidRequest, PrincipalRequired, refuse_nul
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
from epistrel.core.media import assets as media
from epistrel.core.media import uploads as media_uploads
from epistrel.core.media.poster import FfmpegTools, PosterExtractor
from epistrel.core.media.signing import Signer
from epistrel.core.media.store import BlobStore
from epistrel.core.media.stores import make_store
from epistrel.core.policies.model import Policy, Scope
from epistrel.core.ports import Ports
from epistrel.core.profiles import effective, impact
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.impact import ImpactPreviewRequest, ImpactReport, TwoStageResult
from epistrel.core.profiles.schema import Purposes
from epistrel.core.profiles.validate import ProfileValidation
from epistrel.core.settings_schema import ConfigClass, StorySettings
from epistrel.core.tables import events
from epistrel.core.tables.media import MediaKind, MediaState
from epistrel.core.tables.ownership import EntityType
from epistrel.core.turn_gate import Concurrency, QueueItem
from epistrel.core.viewpoint import is_omniscient, resolve_viewpoint, with_requested
from epistrel.observability import instrument
from epistrel.orchestrator import queue_drainer
from epistrel.orchestrator.budget import (
    DEFAULT_TURN_DEADLINE_S,
    GUARDRAIL_INPUT,
    GUARDRAIL_OUTPUT,
    TurnBudget,
    effective_max_calls,
)
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.orchestrator.jobs.handlers import lite_summary as lite_summary_job
from epistrel.orchestrator.jobs.handlers import media_sweep as media_sweep_job
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
from epistrel.orchestrator.models import facts as model_facts
from epistrel.orchestrator.models import metrics as model_metrics
from epistrel.orchestrator.models import registry as model_registry
from epistrel.orchestrator.policies import admin as policy_admin
from epistrel.orchestrator.policies import input_gate, output_gate
from epistrel.orchestrator.policies import store as policy_store
from epistrel.orchestrator.policies.age import CastMember
from epistrel.orchestrator.policies.catalog_check import CatalogPolicyCheck
from epistrel.orchestrator.policies.input_gate import EntryPoint, GateText, InputPass
from epistrel.orchestrator.policies.schemas import (
    PolicyFields,
    PolicyReplacement,
    StoryPolicyItem,
)
from epistrel.testing import crash

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
    "create_character_draft": ResultCodec.of(
        catalog_characters.DraftWrite, status=201, revision_field=_CONFIG
    ),
    "save_character_draft": ResultCodec.of(catalog_characters.DraftWrite, revision_field=_CONFIG),
    "publish_character": ResultCodec.of(
        catalog_characters.Published, status=201, revision_field=_CONFIG
    ),
    "archive_character": ResultCodec.of(catalog_characters.StatusChange, revision_field=_CONFIG),
    "unarchive_character": ResultCodec.of(catalog_characters.StatusChange, revision_field=_CONFIG),
    "delete_character": ResultCodec.of(
        catalog_characters.DeletedCharacter, status=204, revision_field=_CONFIG
    ),
    "create_scenario_draft": ResultCodec.of(
        catalog_scenarios.ScenarioDraftWrite, status=201, revision_field=_CONFIG
    ),
    "save_scenario_draft": ResultCodec.of(
        catalog_scenarios.ScenarioDraftWrite, revision_field=_CONFIG
    ),
    "publish_scenario": ResultCodec.of(
        catalog_scenarios.Published, status=201, revision_field=_CONFIG
    ),
    "archive_scenario": ResultCodec.of(
        catalog_scenarios.ScenarioStatusChange, revision_field=_CONFIG
    ),
    "unarchive_scenario": ResultCodec.of(
        catalog_scenarios.ScenarioStatusChange, revision_field=_CONFIG
    ),
    "delete_scenario": ResultCodec.of(
        catalog_scenarios.DeletedScenario, status=204, revision_field=_CONFIG
    ),
    "upload_media": ResultCodec.of(media.MediaWrite, status=201, revision_field=_CONFIG),
    "delete_media": ResultCodec.of(media.MediaRetirement, status=204, revision_field=_CONFIG),
    # Resumable uploads (#107). A part PUT (``put_upload_part``) keeps no result: sending a part
    # again replaces it, so the request is idempotent by itself.
    "open_upload": ResultCodec.of(media_uploads.UploadOpened, status=201, revision_field=_CONFIG),
    "complete_upload": ResultCodec.of(media.MediaWrite, status=201, revision_field=_CONFIG),
    "abort_upload": ResultCodec.of(media_uploads.UploadAborted, status=204, revision_field=_CONFIG),
    # Engine profiles (#106): deployment-scoped like the catalog's writes.
    "create_profile": ResultCodec.of(profiles.ProfileWrite, status=201, revision_field=_CONFIG),
    # Two-stage (#109): stored only once applied; a report alone releases its key.
    "update_profile": ResultCodec.of(TwoStageResult[profiles.ProfileWrite], revision_field=_CONFIG),
    "copy_profile": ResultCodec.of(profiles.ProfileWrite, status=201, revision_field=_CONFIG),
    "delete_profile": ResultCodec.of(profiles.DeletedProfile, revision_field=_CONFIG),
}
"""How each mutating method's result is stored under its ``Idempotency-Key`` and replayed: the
result type, the REST status it answers with, and the revision a reference carries."""


def _ownership_codecs() -> dict[str, ResultCodec[Any]]:
    """The ownership writes' codecs, one per method and owned type (#104): the key is the
    route's ``operation_id`` (``transfer_owner_story``). A story's writes carry its
    ``timeline_revision``; every other type's the deployment's ``config_revision``."""
    codecs: dict[str, ResultCodec[Any]] = {}
    for entity_type in EntityType:
        revision = "timeline_revision" if entity_type is EntityType.STORY else _CONFIG
        suffix = entity_type.value
        codecs[f"transfer_owner_{suffix}"] = ResultCodec.of(
            ownership.OwnerTransfer, revision_field=revision
        )
        codecs[f"add_relation_{suffix}"] = ResultCodec.of(
            ownership.RelationWrite,
            status=lambda write: 201 if write.created else 200,
            revision_field=revision,
        )
        codecs[f"remove_relation_{suffix}"] = ResultCodec.of(
            ownership.RelationRemoved, status=204, revision_field=revision
        )
    return codecs


CODECS.update(_ownership_codecs())
# A catalog entry's listing status (#110): one method, one codec per catalog kind (the key is the
# route's ``operation_id``, ``set_listing_status_character``), deployment-scoped.
CODECS.update(
    {
        f"set_listing_status_{kind.value}": ResultCodec.of(
            catalog_listing.ListingStatusChange, revision_field=_CONFIG
        )
        for kind in catalog_kinds.SERVICES
    }
)

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
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body", "policies")) from None


LIBRARY_ONLY_ATTRIBUTE = "__epistrel_library_only__"


def library_only[F: Callable[..., Any]](reason: str) -> Callable[[F], F]:
    """Mark an ``Engine`` coroutine method that has no REST route of its own, and say why.

    Every other public coroutine method of ``Engine`` is an *operation*: the REST route that
    serves it carries ``operation_id`` = the method's name (``<name>_<suffix>`` when several
    routes serve it), and C-03's parity test runs it over both surfaces (FR-API-3; #73)."""

    def mark(method: F) -> F:
        setattr(method, LIBRARY_ONLY_ATTRIBUTE, reason)
        return method

    return mark


def operations(engine_class: type[Engine] | None = None) -> list[str]:
    """The public operations of ``engine_class`` (``Engine``): every public coroutine method not
    marked ``library_only``, sorted by name."""
    cls = engine_class or Engine
    found: list[str] = []
    for name in dir(cls):
        if name.startswith("_"):
            continue
        method = inspect.getattr_static(cls, name)
        if isinstance(method, staticmethod | classmethod):
            continue
        if inspect.iscoroutinefunction(method) and not hasattr(method, LIBRARY_ONLY_ATTRIBUTE):
            found.append(name)
    return sorted(found)


def library_only_methods(engine_class: type[Engine] | None = None) -> dict[str, str]:
    """``{method: reason}`` for every ``library_only`` coroutine method of ``engine_class``."""
    cls = engine_class or Engine
    marked: dict[str, str] = {}
    for name in dir(cls):
        reason = getattr(inspect.getattr_static(cls, name), LIBRARY_ONLY_ATTRIBUTE, None)
        if isinstance(reason, str):
            marked[name] = reason
    return marked


class _Metered(Protocol):
    @property
    def model_calls(self) -> tuple[dict[str, Any], ...]: ...

    @property
    def elapsed_ms(self) -> int: ...


async def _metered[R: _Metered](ctx: RequestContext, run: Callable[[], Awaitable[R]]) -> R:
    """``run``'s result with the model calls made inside it and the time since the request
    arrived (``ctx.received_at``, else now): ingest and the story-text edits report what their
    input gate spent, as a turn does (#69's hand-off; #73)."""
    started = ctx.received_at if ctx.received_at is not None else time.monotonic()
    with model_metrics.recording() as log:
        value = await run()
    return dataclasses.replace(
        value,  # type: ignore[type-var]
        model_calls=tuple(call.model_dump(mode="json") for call in log.calls),
        elapsed_ms=round((time.monotonic() - started) * 1000),
    )


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
        ports: Ports | None = None,
    ) -> None:
        self.settings = settings
        self._db = db
        # Every model call goes through this one layer (FR-MODEL-1); the adapter is chosen by
        # EPISTREL_MODEL_ADAPTER. Tests reach the stub through ``engine.models.stub``.
        self.models = models or ModelAccessLayer.from_settings(settings)
        self.models.bind(db)
        # What Core asks of the Orchestrator (#103): the input gate, policy evaluation, model
        # calls and background jobs. The policy evaluation is the Orchestrator's (#111: the
        # catalog validation's input policies); the other no-op defaults stand until a story
        # wires a real one.
        self.ports = ports or Ports(policy_evaluator=CatalogPolicyCheck(self.models))
        # Queued turns (FR-CONC-5, #63) are drained in this process: kicked when queued here, and
        # by the worker's ``turn_queue`` recovery hook and ``turn_queue_drain`` task (#71).
        self._recovery: asyncio.Task[None] | None = None
        self.worker: Worker | None = None
        self._worker_task: asyncio.Task[None] | None = None
        self.drainer = queue_drainer.QueueDrainer(
            lambda: self.db,
            {turn_gate.RESPOND: self._drain_respond, turn_gate.INGEST: self._drain_ingest},
        )
        # Media bytes (#105, #107): the blob store EPISTREL_MEDIA_STORE names (local under
        # EPISTREL_MEDIA_ROOT, or S3; prepared at start-up by ``prepare_media``), the signer of
        # content URLs and of the moderation hook's requests, and the ffmpeg seam that takes a
        # video's poster from its first frame and probes videos (tests replace it).
        self.media: BlobStore = make_store(settings)
        self.signer = Signer(settings.config_key)
        self.posters: PosterExtractor = FfmpegTools(settings.ffmpeg_path)

    @classmethod
    def from_settings(cls, settings: Settings) -> Engine:
        return cls(settings)

    def bind(self, db: AsyncEngine | None) -> None:
        """Use ``db`` for every operation (``None`` unbinds)."""
        self._db = db
        self.models.bind(db)

    @library_only("lifecycle: the REST app starts it at start-up")
    async def start_workers(self, mode: ProcessMode | None = None) -> None:
        """Start the in-process background work (the app's start-up; NFR-REL-5, #71).

        ``all`` and ``worker`` modes run the job worker (``worker_concurrency`` loops) as an
        asyncio task: its recovery pass drains every turn queue a stopped process left behind
        (the ``turn_queue`` recovery hook), and its ``turn_queue_drain`` task re-checks every
        30 s. The ``lite_summary`` job calls models through this Engine's layer (#72). ``api``
        mode runs no worker and only drains the queues left behind once. Either
        way it runs in the background, so an unreachable database never stops the server."""
        mode = mode or self.settings.mode
        turn_queue_job.bind(self.drainer.recover)
        lite_summary_job.bind(self.models)
        media_sweep_job.bind(self.media)
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

    @library_only("lifecycle: the REST app stops it at shutdown")
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
        if lite_summary_job.bound() is self.models:
            lite_summary_job.bind(None)
        if media_sweep_job.bound() is self.media:
            media_sweep_job.bind(None)
        await self.drainer.stop()

    @property
    def db(self) -> AsyncEngine:
        if self._db is None:
            raise RuntimeError("the Engine has no database; bind one first")
        return self._db

    def prepare_media(self) -> None:
        """Create the media root and its staging area and prove them writable, and on S3 that
        the bucket exists (the app's and ``epistrel serve``'s start-up; ``MediaRootUnwritable``
        otherwise, exit 78)."""
        self.media.prepare()

    async def _keyed[T](
        self,
        ctx: RequestContext,
        operation: str,
        run: Callable[[RequestContext], Awaitable[T]],
        *,
        story_id: str | None = None,
        material: Mapping[str, Any] | None = None,
        respond_async: bool = False,
        keep: Callable[[T], bool] | None = None,
    ) -> T:
        """Run ``run`` as one idempotent call (see the class docstring).

        ``story_id`` keys the call per story (else per deployment). ``material`` is what the
        request hash covers besides the operation name and the expected revision; ``None``
        means ``run`` claims the key itself at its slot in the precheck order (respond, whose
        hash needs the resolved viewpoint). A retry whose original waits in the story's turn
        queue waits for that turn (#63), like the original: up to ``turn_deadline``, or not at
        all with ``respond_async`` (then ``TurnAccepted``, REST 202). ``keep``: whether a
        result is stored under the key; one it rejects (a two-stage report that applied
        nothing, #109) releases the key instead, so the call leaves no trace."""
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
        if material is not None:
            refuse_nul(material)
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
            if keep is not None and not keep(value):
                await call.release()
                return value
            # Committed, not yet answered (#75): a crash here leaves the key for the retry.
            crash.hit("after_commit_before_response", operation)
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
        the turn's commit binds it and its result is stored there for the waiting requests.
        It is a unit of background work of its own (``<operation>_queued``; #74)."""
        with instrument.background(
            f"{operation}_queued",
            story_id=item.story_id,
            operation_id=item.operation_id,
            principal=item.principal,
        ):
            await self._run_queued(item, operation, run)

    async def _run_queued(
        self, item: QueueItem, operation: str, run: Callable[[], Awaitable[Any]]
    ) -> None:
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

    @library_only("the trust boundary's stand-in; served only by test-registered routes")
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

    @library_only("a precondition every story mutation runs (`X-Epistrel-Expected-Revision`)")
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
        include_summary: bool = False,
    ) -> History:
        """The story's corrected history through ``as_of`` (default the head), produced for one
        output viewpoint (FR-RESP-12): ``viewpoint`` (or ``ctx.viewpoint``) when named, else
        ``actor_character_id``, else the story's default; 422 ``viewpoint_unresolved`` when none
        resolves. A character viewpoint never gets ``think``/``latent`` beats; audit records
        (``include_audit``) need ``omniscient`` by name (403 ``omniscient_required``). Pages of
        whole turns, newest first, up to ``limit`` items; ``before_turn`` pages backwards.
        ``variant`` reads along that chain instead of the active one (#66);
        ``include_summary`` adds the included rolling-summary segments (#72)."""
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
            include_summary=include_summary,
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

    @library_only("the input gate inside respond, ingest, the story-text edits and story creation")
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
        refuse_nul({"story_id": story_id, "body": request.model_dump(mode="json")})
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

            return await _metered(
                ctx,
                lambda: ingest.ingest(
                    self.db,
                    story_id,
                    request,
                    principal=principal,
                    gate=gate,
                    idempotency_key=ctx.idempotency_key,
                    expected_revision=ctx.expected_revision,
                    queue_batch=queue_batch,
                ),
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
            return await _metered(
                ctx,
                lambda: story_texts.set_pinned_memories(
                    self.db,
                    story_id,
                    request,
                    principal=principal,
                    gate=self._text_gate(ctx, story_id, EntryPoint.PINNED_MEMORIES),
                    idempotency_key=ctx.idempotency_key,
                    expected_revision=ctx.expected_revision,
                ),
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
            return await _metered(
                ctx,
                lambda: story_texts.set_custom_instructions(
                    self.db,
                    story_id,
                    request,
                    principal=principal,
                    gate=self._text_gate(ctx, story_id, EntryPoint.CUSTOM_INSTRUCTIONS),
                    idempotency_key=ctx.idempotency_key,
                    expected_revision=ctx.expected_revision,
                ),
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

    @library_only("served as `effective_budget` in `GET`/`PATCH /stories/{id}/settings`")
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

    @library_only("served as `undo_available`/`dormant_chains` in `GET /stories/{id}`")
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
        self,
        ctx: RequestContext,
        *,
        status: StoryStatus | None = None,
        owner: str | None = None,
        relation: Sequence[str] = (),
    ) -> list[stories.StoryView]:
        """Every story, newest first, unless the request filters: by ``status``, by ``owner``
        (a principal), or by ``relation`` (each ``name:principal``, or ``name:`` for any
        principal; repeated filters all hold). Owners and relations narrow a list only when
        asked (FR-AUTH-8)."""
        return await stories.list_stories(
            self.db,
            status=status,
            owner=ownership.owner_filter(owner),
            relations=[ownership.parse_relation_filter(value) for value in relation],
        )

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

    @library_only("served as `effective_policies` in `GET /stories/{id}/settings`")
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

    @library_only("served as `policies` in `PATCH /stories/{id}/settings`")
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

    # -- the character catalog (FR-CAT-1,3,9; FR-SCN-10; FR-API-6; #103) ----------------------
    # Deployment-scoped: each write is one transaction recording a story-less ``control``
    # operation, keyed per deployment. Nothing here reads or changes a story (FR-CAT-9).

    async def create_character_draft(
        self, ctx: RequestContext, request: catalog_characters.CharacterCreate
    ) -> catalog_characters.DraftWrite:
        """Create a catalog character whose draft holds ``request``'s fields, at draft revision
        1 and status ``draft`` (FR-CAT-1,3), owned by ``request.owner`` or else the requesting
        principal (FR-AUTH-5). Answers the draft in ``full``."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "create_character_draft",
            lambda _: catalog_characters.create(self.db, request, principal=principal),
            material={"body": _body(request)},
        )

    async def save_character_draft(
        self,
        ctx: RequestContext,
        character_id: str,
        patch: catalog_characters.CharacterPatch,
    ) -> catalog_characters.DraftWrite:
        """Apply a JSON merge patch to the draft (``null`` removes a field), compared on
        ``patch.draft_revision``: a stale revision is 409 ``draft_stale`` and changes nothing
        (FR-CAT-3); an archived entry is 422 ``entry_archived``. Bumps the draft revision."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "save_character_draft",
            lambda _: catalog_characters.save(self.db, character_id, patch, principal=principal),
            material={
                "character_id": character_id,
                "body": patch.model_dump(mode="json", exclude_unset=True),
            },
        )

    async def publish_character(
        self,
        ctx: RequestContext,
        character_id: str,
        request: catalog_characters.PublishRequest,
    ) -> catalog_characters.Published:
        """Publish the draft as an immutable version ``n + 1`` (FR-CAT-3), compared on
        ``request.draft_revision`` like a save; 422 ``publish_invalid`` with the report when the
        draft cannot be published. The draft stays equal to what was published, at the next
        revision."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "publish_character",
            lambda _: catalog_characters.publish(
                self.db, character_id, request, principal=principal, ports=self.ports
            ),
            material={"character_id": character_id, "body": _body(request)},
        )

    async def archive_character(
        self, ctx: RequestContext, character_id: str
    ) -> catalog_characters.StatusChange:
        """Archive the entry: it still reads, but cannot be edited, published, or used to create
        a story (#119) until it is unarchived. Archiving an archived entry changes nothing."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "archive_character",
            lambda _: catalog_characters.archive(self.db, character_id, principal=principal),
            material={"character_id": character_id},
        )

    async def unarchive_character(
        self, ctx: RequestContext, character_id: str
    ) -> catalog_characters.StatusChange:
        """Restore an archived entry to ``published`` (``draft`` if never published); any other
        entry is left as it is."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "unarchive_character",
            lambda _: catalog_characters.unarchive(self.db, character_id, principal=principal),
            material={"character_id": character_id},
        )

    async def delete_character(
        self, ctx: RequestContext, character_id: str
    ) -> catalog_characters.DeletedCharacter:
        """Delete the entry and its versions, from any status, with a ``catalog_entry_deleted``
        audit row. Stories created from it keep their snapshot and provenance (FR-CAT-9)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "delete_character",
            lambda _: catalog_characters.delete(self.db, character_id, principal=principal),
            material={"character_id": character_id},
        )

    async def get_character(
        self,
        ctx: RequestContext,
        character_id: str,
        *,
        version: int | None = None,
        draft: bool = False,
        projection: catalog_characters.Projection = catalog_characters.Projection.CARD,
    ) -> catalog_characters.CharacterCard | catalog_characters.CharacterFull:
        """The latest published version (``version``: that one; ``draft``: the draft) as its
        public ``card`` (the default) or in ``full`` (FR-SCN-10). 422 ``not_published`` before
        the first publish (read the draft instead); 404 for an unknown entry or version."""
        return await catalog_characters.get(
            self.db, character_id, version=version, draft=draft, projection=projection
        )

    async def list_character_versions(
        self, ctx: RequestContext, character_id: str
    ) -> list[catalog_characters.VersionSummary]:
        """Every published version of the entry, oldest first: number, time and publisher."""
        return await catalog_characters.versions(self.db, character_id)

    # -- the scenario catalog (FR-SCN-1..4,10; FR-API-6; #108) --------------------------------
    # The same lifecycle as the character catalog's (``core.catalog.service``), deployment-scoped
    # the same way. Nothing here reads or changes a story (FR-CAT-9).

    async def create_scenario_draft(
        self, ctx: RequestContext, request: scenario_models.ScenarioCreate
    ) -> catalog_scenarios.ScenarioDraftWrite:
        """Create a scenario whose draft holds ``request``'s groups, at draft revision 1 and
        status ``draft`` (FR-SCN-1), owned by ``request.owner`` or else the requesting
        principal (FR-AUTH-5). The save rules (roles, references, seeds) apply: 422
        ``validation_error`` at the field; 422 ``cast_limit_exceeded`` above the deployment's
        ceiling (FR-SCN-4); 422 ``rating_retired``/``media_retired`` for a rating or media asset
        that cannot be referenced. Answers the draft in ``full``."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "create_scenario_draft",
            lambda _: catalog_scenarios.create(self.db, request, principal=principal),
            material={"body": _body(request)},
        )

    async def save_scenario_draft(
        self,
        ctx: RequestContext,
        scenario_id: str,
        patch: scenario_models.ScenarioPatch,
    ) -> catalog_scenarios.ScenarioDraftWrite:
        """Apply a JSON merge patch to the draft (``null`` removes a field, an array replaces
        the stored one), compared on ``patch.draft_revision``: a stale revision is 409
        ``draft_stale`` and changes nothing (FR-CAT-3); an archived scenario is 422
        ``entry_archived``. The merged draft keeps the save rules. Bumps the draft revision."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "save_scenario_draft",
            lambda _: catalog_scenarios.save(self.db, scenario_id, patch, principal=principal),
            material={
                "scenario_id": scenario_id,
                "body": patch.model_dump(mode="json", exclude_unset=True),
            },
        )

    async def publish_scenario(
        self,
        ctx: RequestContext,
        scenario_id: str,
        request: catalog_scenarios.PublishRequest,
    ) -> catalog_scenarios.Published:
        """Publish the draft as an immutable version ``n + 1`` (FR-SCN-1), compared on
        ``request.draft_revision`` like a save. The draft is validated (FR-SCN-5; #111): any
        error is 422 ``publish_invalid`` with ``details.report`` and nothing is written; the
        answer carries the report's warnings and the checks this deployment cannot run, and the
        version keeps the report. The draft stays equal to what was published, at the next
        revision."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "publish_scenario",
            lambda _: catalog_scenarios.publish(
                self.db, scenario_id, request, principal=principal, ports=self.ports
            ),
            material={"scenario_id": scenario_id, "body": _body(request)},
        )

    async def validate_scenario(
        self,
        ctx: RequestContext,
        scenario_id: str,
        request: catalog_scenarios.ValidateRequest | None = None,
        *,
        version: int | None = None,
    ) -> ValidationResult:
        """Validate the scenario's draft, or its published version ``version``, as a dry run
        (FR-SCN-5; #111): ``{valid, report}``, the report the same shape a publish answers,
        every check this deployment can run, and the others listed ``unavailable``. It writes
        nothing (no operation, version or key), so it takes no ``Idempotency-Key``. 404 for an
        unknown scenario or version; 409 ``draft_stale`` when ``request.draft_revision`` is no
        longer the draft's; 422 when both are named. Archived scenarios validate too."""
        return await catalog_scenarios.validate(
            self.db, scenario_id, request, version=version, ports=self.ports
        )

    async def archive_scenario(
        self, ctx: RequestContext, scenario_id: str
    ) -> catalog_scenarios.ScenarioStatusChange:
        """Archive the scenario: it still reads, but cannot be edited, published, or used to
        create a story (#119) until it is unarchived. Archiving an archived one changes
        nothing."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "archive_scenario",
            lambda _: catalog_scenarios.archive(self.db, scenario_id, principal=principal),
            material={"scenario_id": scenario_id},
        )

    async def unarchive_scenario(
        self, ctx: RequestContext, scenario_id: str
    ) -> catalog_scenarios.ScenarioStatusChange:
        """Restore an archived scenario to ``published`` (``draft`` if never published); any
        other is left as it is."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "unarchive_scenario",
            lambda _: catalog_scenarios.unarchive(self.db, scenario_id, principal=principal),
            material={"scenario_id": scenario_id},
        )

    async def delete_scenario(
        self, ctx: RequestContext, scenario_id: str
    ) -> catalog_scenarios.DeletedScenario:
        """Delete the scenario and its versions, from any status, with a
        ``catalog_entry_deleted`` audit row. Stories created from it keep their snapshot and
        provenance (FR-CAT-9)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "delete_scenario",
            lambda _: catalog_scenarios.delete(self.db, scenario_id, principal=principal),
            material={"scenario_id": scenario_id},
        )

    async def get_scenario(
        self,
        ctx: RequestContext,
        scenario_id: str,
        *,
        version: int | None = None,
        draft: bool = False,
        projection: catalog_scenarios.Projection = catalog_scenarios.Projection.CARD,
    ) -> scenario_models.ScenarioCard | scenario_models.ScenarioFull:
        """The latest published version (``version``: that one; ``draft``: the draft) as its
        public ``card`` (the default) or in ``full`` (FR-SCN-10). 422 ``not_published`` before
        the first publish (read the draft instead); 404 for an unknown scenario or version."""
        return await catalog_scenarios.get(
            self.db, scenario_id, version=version, draft=draft, projection=projection
        )

    async def list_scenario_versions(
        self, ctx: RequestContext, scenario_id: str
    ) -> list[catalog_scenarios.VersionSummary]:
        """Every published version of the scenario, oldest first: number, time and publisher."""
        return await catalog_scenarios.versions(self.db, scenario_id)

    # -- listing status and listings (FR-CAT-11, FR-API-6, FR-AUTH-8; #110) ----------------------
    # One method per operation for both catalog kinds (``entity_type`` ``character`` or
    # ``scenario``), each served by one route per kind. Nothing here gates on the listing status,
    # an owner, a relation or a label: a list filters by them only when asked.

    async def set_listing_status(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        entity_id: str,
        request: catalog_listing.ListingStatusRequest,
    ) -> catalog_listing.ListingStatusChange:
        """Set a catalog entry's listing status (FR-CAT-11) to ``request.value``, one of the
        deployment's ``listing_status_values`` (422 ``invalid_listing_status`` otherwise,
        checked before the call's ``Idempotency-Key`` is claimed). The change is recorded with
        the acting principal and time; a character's companion scenarios change with it, and a
        companion cannot change alone (422 ``companion_follows_character``). The current value
        changes nothing. Allowed in any status. The value gates nothing (FR-AUTH-8)."""
        principal = self.require_principal(ctx)
        service = catalog_kinds.service_of(entity_type)
        await catalog_listing.require_allowed(self.db, request.value)
        return await self._keyed(
            ctx,
            f"set_listing_status_{service.entity_type.value}",
            lambda _: service.set_listing_status(
                self.db, entity_id, request.value, principal=principal
            ),
            material={
                "entity_type": service.entity_type.value,
                "entity_id": entity_id,
                "body": _body(request),
            },
        )

    async def search_catalog(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        *,
        q: str | None = None,
        label: Sequence[str] = (),
        content_rating: str | None = None,
        listing_status: str | None = None,
        owner: str | None = None,
        relation: Sequence[str] = (),
        status: EntryStatus | str | None = None,
        include_companions: bool = False,
        limit: int = catalog_search.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> catalog_search.CatalogPage:
        """A page of catalog characters or scenarios as cards (FR-API-6): the ``published``
        ones (``status``: the ``draft`` or ``archived`` ones), newest first, every owner and
        listing status included. ``q`` searches the latest published version's public card
        (best match first, with each card's ``rank``); ``label`` (all must hold),
        ``content_rating``, ``listing_status``, ``owner`` and ``relation`` (``name:principal``,
        or ``name:`` for any principal; all must hold) narrow the list only when given
        (FR-AUTH-8). Companion scenarios are listed only with ``include_companions``.
        ``cursor`` (a page's ``next_cursor``) continues with the same filters."""
        service = catalog_kinds.service_of(entity_type)
        query = catalog_search.catalog_query(
            q=q,
            labels=label,
            content_rating=content_rating,
            listing_status=listing_status,
            owner=owner,
            relations=relation,
            status=status,
            include_companions=include_companions,
        )
        return await catalog_search.search(self.db, service, query, limit=limit, cursor=cursor)

    # -- ownership (FR-AUTH-5,6,8; #104) --------------------------------------------------------
    # One owner and any number of relations per story, catalog character, scenario, persona,
    # media asset and engine profile: recorded for listing, attribution and offboarding, and
    # never consulted by any other call. Keyed per deployment; allowed in any state (an
    # archived story, a turn in flight). On a story, the expected revision is checked first.

    async def _ownership_write[T](
        self,
        ctx: RequestContext,
        method: str,
        entity_type: EntityType,
        entity_id: str,
        write: Callable[[str], Awaitable[T]],
        material: Mapping[str, Any],
    ) -> T:
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> T:
            if entity_type is EntityType.STORY:
                await self.check_expected_revision(ctx, entity_id)
            return await write(principal)

        return await self._keyed(
            ctx,
            f"{method}_{entity_type.value}",
            run,
            material={"entity_type": entity_type.value, "entity_id": entity_id, **material},
        )

    async def transfer_owner(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        entity_id: str,
        request: ownership.OwnerTransferRequest,
    ) -> ownership.OwnerTransfer:
        """Make ``request.principal`` the entity's owner (FR-AUTH-5), recorded with the acting
        principal and time. A character's companion scenario moves with it; a companion cannot
        be transferred alone (422 ``companion_follows_character``). A transfer to the current
        owner changes nothing. Ownership confers no permission (FR-AUTH-8)."""
        self.require_principal(ctx)
        kind = ownership.entity_type_of(entity_type)
        return await self._ownership_write(
            ctx,
            "transfer_owner",
            kind,
            entity_id,
            lambda principal: ownership.transfer_owner(
                self.db, kind, entity_id, request.principal, principal=principal
            ),
            {"body": _body(request)},
        )

    async def add_relation(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        entity_id: str,
        request: ownership.RelationRequest,
    ) -> ownership.RelationWrite:
        """Relate ``request.principal`` to the entity as ``request.relation`` (FR-AUTH-6),
        recorded with the acting principal; a relation that exists is answered as it is. The
        name is never interpreted: a relation grants nothing (FR-AUTH-8)."""
        self.require_principal(ctx)
        kind = ownership.entity_type_of(entity_type)
        return await self._ownership_write(
            ctx,
            "add_relation",
            kind,
            entity_id,
            lambda principal: ownership.add_relation(
                self.db,
                kind,
                entity_id,
                request.principal,
                request.relation,
                principal=principal,
            ),
            {"body": _body(request)},
        )

    async def remove_relation(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        entity_id: str,
        relation: str,
        principal: str,
    ) -> ownership.RelationRemoved:
        """Remove the entity's ``relation`` to ``principal``, recorded with the acting
        principal; 404 ``not_found`` when there is no such relation."""
        self.require_principal(ctx)
        kind = ownership.entity_type_of(entity_type)
        name = ownership.path_relation(relation)
        related = ownership.path_principal(principal)
        return await self._ownership_write(
            ctx,
            "remove_relation",
            kind,
            entity_id,
            lambda actor: ownership.remove_relation(
                self.db, kind, entity_id, related, name, principal=actor
            ),
            {"relation": name, "principal": related},
        )

    async def list_relations(
        self, ctx: RequestContext, entity_type: EntityType | str, entity_id: str
    ) -> ownership.Relations:
        """The entity's owner and its relations, by relation name, then principal."""
        return await ownership.list_relations(
            self.db, ownership.entity_type_of(entity_type), entity_id
        )

    # -- media assets (FR-MEDIA-1,2; FR-API-6; #105) --------------------------------------------
    # Deployment-scoped: an upload or a delete records a story-less ``control`` operation, keyed
    # per deployment. Media are referenced by id and never touch a story.

    async def upload_media(
        self,
        ctx: RequestContext,
        request: media.MediaUpload,
        content: bytes | AsyncIterable[bytes],
        *,
        content_type: str | None = None,
        body_size: int | None = None,
        poster_file: media.PosterFileSource = None,
    ) -> media.MediaWrite:
        """Store an image or video as an immutable asset (FR-MEDIA-1): ``request`` is its
        metadata (``depicted_subjects`` required: 422 ``subjects_required``), ``content`` its
        bytes (or chunks), ``content_type`` the bytes' declared MIME type and ``body_size`` the
        whole request's declared length (REST's ``Content-Length``). 415
        ``media_type_not_allowed``, 413 ``media_too_large``, 413 ``use_multipart`` (larger than
        ``media_multipart_threshold``: use ``open_upload``), 422 ``rating_retired``; with a
        moderation hook configured, 422 ``moderation_rejected`` or 503
        ``moderation_unavailable`` (nothing stored). A video's poster (FR-MEDIA-3) is
        ``request.poster``, the ``poster_file`` given, or extracted from its first frame with
        ffmpeg (422 ``poster_required`` without it, ``poster_extraction_failed`` when it fails).
        The key's request hash covers the metadata and the bytes' SHA-256
        (``core.media.assets``)."""
        principal = self.require_principal(ctx)
        request = media.check_upload(request)
        settings = await media.read_media_settings(self.db)
        threshold = settings.media_multipart_threshold
        staged = await media.stage(
            self.media,
            new_ulid(),
            media.content_of(content),
            settings=settings,
            kind=request.kind,
            content_type=content_type,
            body_size=body_size,
            threshold=threshold,
        )
        poster: media.Staged | None = None
        try:
            extra = await media.poster_file_of(poster_file)
            if extra is not None:
                if request.kind is not MediaKind.VIDEO or request.poster is not None:
                    raise InvalidRequest(
                        "a `poster_file` is a video's poster, sent instead of `poster`",
                        {"field": f"body.{media.POSTER_FILE_FIELD}"},
                    )
                poster = await media.stage(
                    self.media,
                    new_ulid(),
                    media.content_of(extra.content),
                    settings=settings,
                    kind=MediaKind.IMAGE,
                    content_type=extra.content_type,
                    threshold=threshold,
                    counted=staged.size,
                )
            material: dict[str, Any] = {
                "body": _body(request),
                "sha256": staged.sha256,
                "size": staged.size,
                "mime_type": staged.mime_type,
            }
            if poster is not None:
                material["poster_file"] = {
                    "sha256": poster.sha256,
                    "size": poster.size,
                    "mime_type": poster.mime_type,
                }
            return await self._keyed(
                ctx,
                "upload_media",
                lambda _: media.upload(
                    self.db,
                    self.media,
                    staged,
                    request,
                    principal=principal,
                    settings=settings,
                    signer=self.signer,
                    posters=self.posters,
                    poster_file=poster,
                ),
                material=material,
            )
        finally:
            await media.discard(staged)
            await media.discard(poster)

    async def get_media(self, ctx: RequestContext, media_id: str) -> media.MediaAsset:
        """An asset's metadata, ``live`` or ``retired``; 404 for an unknown asset."""
        return await media.get(self.db, media_id)

    async def get_media_content(
        self,
        ctx: RequestContext,
        media_id: str,
        *,
        exp: int | None = None,
        sig: str | None = None,
        byte_range: media.ByteRange | None = None,
        if_none_match: Sequence[str] = (),
        if_range: str | None = None,
        with_bytes: bool = True,
    ) -> media.MediaContent:
        """An asset's bytes, opened (read them with ``chunks()`` or ``read()``): served for a
        ``live`` or ``retired`` asset, so whatever references a deleted asset keeps showing it
        (FR-MEDIA-2). ``byte_range`` selects one range (FR-MEDIA-3; 416
        ``range_not_satisfiable`` when no byte of it exists), applied only when ``if_range``
        is absent or names the current entity tag; ``if_none_match`` matching the entity tag
        answers ``not_modified`` with no bytes; ``with_bytes`` false opens nothing (``HEAD``).
        ``exp`` and ``sig`` are a signed URL's (``get_media_url``, or the moderation hook's
        ``content_url``): when they verify, an upload still waiting for the hook is served
        too. 404 otherwise."""
        signed = (
            exp is not None
            and sig is not None
            and self.signer.verify("GET", media.content_path(media_id), exp, sig)
        )
        return await media.open_content(
            self.db,
            self.media,
            media_id,
            signed=signed,
            byte_range=byte_range,
            if_none_match=if_none_match,
            if_range=if_range,
            with_bytes=with_bytes,
        )

    async def get_media_url(
        self, ctx: RequestContext, media_id: str, *, ttl: int | None = None
    ) -> media.MediaUrl:
        """A short-lived URL that serves the asset's bytes without the service credential
        (FR-MEDIA-3), valid ``ttl`` seconds (at most, and by default, the deployment's
        ``media_signed_url_ttl``): on S3 the store's own presigned URL, so the store serves the
        bytes; on the local store Epistrel's signed ``/media/{id}/content?exp=&sig=`` under
        ``public_base_url`` (503 ``config_missing`` without it, 503 ``config_key_missing``
        without ``EPISTREL_CONFIG_KEY``). 404 for an unknown asset."""
        settings = await media.read_media_settings(self.db)
        return await media.signed_url(
            self.db, self.media, media_id, ttl_s=ttl, settings=settings, signer=self.signer
        )

    async def list_media(
        self,
        ctx: RequestContext,
        *,
        owner: str | None = None,
        relation: Sequence[str] = (),
        kind: MediaKind | None = None,
        state: MediaState = MediaState.LIVE,
        limit: int = media.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> media.MediaPage:
        """``live`` assets (``state=retired``: the deleted ones), newest first, a page at a time
        (``cursor``). ``owner``, ``relation`` and ``kind`` narrow the list only when given
        (FR-AUTH-8)."""
        return await media.list_assets(
            self.db,
            owner=ownership.owner_filter(owner),
            relations=[ownership.parse_relation_filter(value) for value in relation],
            kind=kind,
            state=state,
            limit=limit,
            cursor=cursor,
        )

    async def delete_media(self, ctx: RequestContext, media_id: str) -> media.MediaRetirement:
        """Delete an asset by retiring it (FR-MEDIA-2): it leaves listings and refuses new
        references (422 ``media_retired``), while everything that already references it keeps
        its content until the media sweep finds no reference and purges it. A video's poster is
        retired with it. Deleting a retired asset changes nothing; 404 for an unknown one."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "delete_media",
            lambda _: media.retire(self.db, media_id, principal=principal),
            material={"media_id": media_id},
        )

    # -- resumable uploads (FR-MEDIA-3; #107) ---------------------------------------------------
    # Deployment-scoped like every media write: open, complete and abort are keyed per
    # deployment; a part is not keyed (sending it again replaces it).

    async def open_upload(
        self, ctx: RequestContext, request: media_uploads.UploadOpen
    ) -> media_uploads.UploadOpened:
        """Open a resumable upload session for a large asset (FR-MEDIA-3): the asset's metadata
        and its declared ``size``, ``mime_type`` and ``sha256``. Answers the ``upload_id`` and
        the ``part_size`` its parts must have (the last: the rest). 413 ``media_too_large``, 415
        ``media_type_not_allowed``, 422 ``rating_retired``, ``subjects_required``,
        ``media_retired``/``poster_invalid`` for a bad ``poster``, ``poster_required`` for a
        video without one when ffmpeg is missing."""
        principal = self.require_principal(ctx)
        settings = await media.read_media_settings(self.db)
        return await self._keyed(
            ctx,
            "open_upload",
            lambda _: media_uploads.open_session(
                self.db,
                self.media,
                request,
                principal=principal,
                settings=settings,
                posters=self.posters,
            ),
            material={"body": _body(request)},
        )

    async def get_upload(self, ctx: RequestContext, upload_id: str) -> media_uploads.UploadSession:
        """A session's state and the parts it has received, so an interrupted upload resumes
        by sending the others. 404 for an unknown session."""
        return await media_uploads.get_session(self.db, upload_id)

    async def put_upload_part(
        self,
        ctx: RequestContext,
        upload_id: str,
        n: int,
        content: bytes | AsyncIterable[bytes],
        *,
        size: int | None = None,
        sha256: str | None = None,
    ) -> media_uploads.PartStored:
        """Store part ``n`` (from 1) of a session: exactly the session's ``part_size`` bytes,
        or the rest for the last part (422 ``part_invalid``), with their ``size`` declared
        (REST's ``Content-Length``; none: 411 ``length_required``) and, optionally, their
        ``sha256`` checked (422 ``checksum_mismatch``). Sending a part again replaces it. 409
        ``upload_closed`` once the session is completed, aborted or expired."""
        self.require_principal(ctx)
        if isinstance(content, bytes | bytearray | memoryview) and size is None:
            size = len(content)
        settings = await media.read_media_settings(self.db)
        return await media_uploads.put_part(
            self.db,
            self.media,
            upload_id,
            n,
            media.content_of(content),
            size=size,
            sha256=sha256,
            settings=settings,
        )

    async def complete_upload(self, ctx: RequestContext, upload_id: str) -> media.MediaWrite:
        """Assemble a session's parts into an asset, through every check ``upload_media``
        makes (FR-MEDIA-1,3): 409 ``upload_incomplete`` (``details.missing``), 422
        ``checksum_mismatch`` (the declared SHA-256), 409 ``upload_closed``, and the upload's
        own refusals. A refusal leaves the session open with its parts."""
        principal = self.require_principal(ctx)
        settings = await media.read_media_settings(self.db)
        return await self._keyed(
            ctx,
            "complete_upload",
            lambda _: media_uploads.complete(
                self.db,
                self.media,
                upload_id,
                principal=principal,
                settings=settings,
                signer=self.signer,
                posters=self.posters,
            ),
            material={"upload_id": upload_id},
        )

    async def abort_upload(
        self, ctx: RequestContext, upload_id: str
    ) -> media_uploads.UploadAborted:
        """Abort an open session: its parts go. A completed or aborted one is left as it is
        (still 204); 404 for an unknown one."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "abort_upload",
            lambda _: media_uploads.abort(self.db, self.media, upload_id, principal=principal),
            material={"upload_id": upload_id},
        )

    # -- engine profiles (FR-PROF-1..4; FR-API-6; #106) -------------------------------------------
    # Deployment-scoped like the catalog: each write is one transaction recording a story-less
    # ``control`` operation, keyed per deployment. Until #113 no turn reads a profile.

    @property
    def purposes(self) -> Purposes:
        """The model purposes a profile may route: the Orchestrator's delivered purposes, and the
        ones it reserves for later milestones (naming one is naming something undelivered)."""
        return Purposes.of(list(model_registry.DELIVERED), list(model_registry.RESERVED))

    async def create_profile(
        self, ctx: RequestContext, request: profiles.ProfileCreate
    ) -> profiles.ProfileWrite:
        """Create an engine profile at version 1 (FR-PROF-1), owned by ``request.owner`` or
        else the requesting principal (FR-AUTH-5). Its settings may name any delivered module;
        what they leave out takes the default. 422 ``validation_error`` for their shape, 422
        ``invalid_profile`` for an undelivered module or level or a missing dependency
        (FR-PROF-2,4); nothing is written."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "create_profile",
            lambda _: profiles.create(
                self.db, request, principal=principal, purposes=self.purposes
            ),
            material={"body": _body(request)},
        )

    async def get_profile(
        self, ctx: RequestContext, profile_id: str, *, version: int | None = None
    ) -> profiles.EngineProfile:
        """The profile at its latest version (``version``: that one), deleted or not; 404 for an
        unknown profile or version."""
        return await profiles.get_profile(self.db, profile_id, version=version)

    async def list_profiles(
        self,
        ctx: RequestContext,
        *,
        owner: str | None = None,
        relation: Sequence[str] = (),
        include_deleted: bool = False,
        limit: int = profiles.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> profiles.ProfilePage:
        """Profiles at their latest version, newest first, presets included; deleted ones only
        with ``include_deleted``. ``owner`` and ``relation`` (``name:principal``, or ``name:``
        for any principal) narrow the list only when given (FR-AUTH-8)."""
        return await profiles.list_profiles(
            self.db,
            owner=ownership.owner_filter(owner),
            relations=[ownership.parse_relation_filter(value) for value in relation],
            include_deleted=include_deleted,
            limit=limit,
            cursor=cursor,
        )

    async def update_profile(
        self, ctx: RequestContext, profile_id: str, request: profiles.ProfileUpdate
    ) -> TwoStageResult[profiles.ProfileWrite]:
        """Replace the profile's name and settings; a new version is made only when the
        settings differ from the latest version's (FR-PROF-1). Presets are edited like any
        profile (FR-PROF-3). Two-stage (FR-PROF-7, #109): the impact report lists every story
        following the profile, and a change it warns of applies only with
        ``request.ignore_warnings``; otherwise the result is the report (``applied`` false),
        nothing is written and the ``Idempotency-Key`` is released. A data-producing increase
        cannot apply to a following story with history: a confirmed edit keeps that story on its
        version. 404 for an unknown profile, 422 ``profile_deleted`` for a deleted one, and the
        settings are refused as on creation, confirmed or not."""
        principal = self.require_principal(ctx)

        async def run(_: RequestContext) -> TwoStageResult[profiles.ProfileWrite]:
            return await profiles.update_profile(
                self.db,
                profile_id,
                request,
                principal=principal,
                facts=await self._runtime_facts(),
                purposes=self.purposes,
            )

        return await self._keyed(
            ctx,
            "update_profile",
            run,
            material={"profile_id": profile_id, "body": _body(request)},
            keep=TwoStageResult.kept,
        )

    async def copy_profile(
        self,
        ctx: RequestContext,
        profile_id: str,
        request: profiles.ProfileCopy,
        *,
        version: int | None = None,
    ) -> profiles.ProfileWrite:
        """A new profile, owned by ``request.owner`` or else the requesting principal, at
        version 1 with the settings of the profile's latest version (``version``: that one's).
        A preset or a deleted profile copies like any other; 404 for an unknown profile or
        version."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "copy_profile",
            lambda _: profiles.copy_profile(
                self.db, profile_id, request, principal=principal, version=version
            ),
            material={"profile_id": profile_id, "version": version, "body": _body(request)},
        )

    async def validate_profile(
        self,
        ctx: RequestContext,
        profile_id: str,
        request: profiles.ProfileValidate | None = None,
    ) -> ProfileValidation:
        """Validate ``request.settings`` (a dry run that writes nothing), or the profile's latest
        version, against the delivered modules and the dependency graph (FR-PROF-2,4): the
        result when valid, else 422 ``invalid_profile`` naming the missing dependencies and
        undelivered settings. 404 for an unknown profile."""
        return await profiles.validate_stored(self.db, profile_id, request, purposes=self.purposes)

    async def delete_profile(self, ctx: RequestContext, profile_id: str) -> profiles.DeletedProfile:
        """Delete the profile (FR-PROF-1): it leaves listings and new assignment, and every
        story that uses it keeps running on its last version until the ``retention_sweep``
        purges it, once no story does. The answer warns of those stories and of the scenarios
        recommending it. The deployment's default cannot be deleted (409, ``reason:
        is_default``); a deleted profile is answered as it is; 404 for an unknown one."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "delete_profile",
            lambda _: profiles.delete_profile(self.db, profile_id, principal=principal),
            material={"profile_id": profile_id},
        )

    async def list_profile_versions(
        self, ctx: RequestContext, profile_id: str
    ) -> list[profiles.ProfileVersion]:
        """Every version of the profile, oldest first: number, time and author."""
        return await profiles.list_versions(self.db, profile_id)

    async def profile_schema(self, ctx: RequestContext) -> profiles.ProfileSchema:
        """What a profile may set on this deployment (FR-PROF-2): the JSON Schema of the
        delivered modules and levels, every delivered setting's three tags, the delivered
        modules with their levels, and the model purposes a profile may route."""
        return profiles.profile_schema(self.purposes)

    # -- a story's configuration: fingerprint, preview (FR-PROF-5,7,16; #109) ------------------

    async def _runtime_facts(self) -> RuntimeFacts:
        """The model each delivered purpose resolves to and the prompt templates' hashes: what
        a fingerprint needs from the Orchestrator."""
        async with self.db.connect() as conn:
            return await model_facts.runtime_facts(conn)

    async def impact_preview(
        self, ctx: RequestContext, story_id: str, request: ImpactPreviewRequest
    ) -> ImpactReport:
        """The impact report of assigning ``request.profile_id`` to the story and/or applying
        ``request.overrides`` (FR-PROF-7), with the fingerprints before and after (FR-PROF-16).
        Writes nothing, so it takes no ``Idempotency-Key``. 404 for an unknown story, 422
        ``profile_deleted`` for a profile that cannot be assigned, 422 ``validation_error`` or
        ``invalid_profile`` for a configuration that cannot run."""
        return await impact.preview(
            self.db,
            story_id,
            request,
            facts=await self._runtime_facts(),
            purposes=self.purposes,
        )

    async def get_effective_config(
        self, ctx: RequestContext, story_id: str
    ) -> effective.EffectiveConfig:
        """The story's effective configuration (FR-PROF-5,16): the profile and the version it
        runs on (``pinned`` when an unavailable upgrade kept an earlier one), every delivered
        setting at its effective value, the fingerprint, the capabilities and the quality
        status. 404 for an unknown story."""
        return await effective.read(self.db, story_id, await self._runtime_facts())


# One span, summary line and count per outermost call of every public coroutine method (#74).
instrument.instrument_engine(Engine)
