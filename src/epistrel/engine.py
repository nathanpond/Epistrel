"""The in-process library: ``epistrel.Engine`` (docs/02 §9, surface 2).

Every REST handler is a thin wrapper that builds a ``RequestContext`` and calls one ``Engine``
method; the library needs no service credential (FR-AUTH-1). The Engine holds its settings and
the database it was bound to (the REST app binds it for the life of the server).
"""

from __future__ import annotations

import asyncio
import collections.abc
import contextlib
import dataclasses
import functools
import hashlib
import inspect
import logging
import time
import types
import typing
from collections.abc import AsyncIterable, Awaitable, Callable, Mapping, Sequence
from datetime import datetime
from typing import Any, Protocol, overload

from pydantic import BaseModel, SecretStr, TypeAdapter, ValidationError
from sqlalchemy import func, select, true
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine

from epistrel.config import ProcessMode, Settings
from epistrel.core import config_changes as config_log
from epistrel.core import (
    config_vocabulary,
    deployment,
    feedback,
    feedback_export,
    feedback_reporting,
    fork,
    idempotency,
    ingest,
    mutation,
    offboarding,
    operation_view,
    ownership,
    principals,
    projections,
    ratings,
    stories,
    story_texts,
    timeline,
    turn_gate,
    variants,
)
from epistrel.core import control as control_ops
from epistrel.core import story_settings as settings_store
from epistrel.core.catalog import characters as catalog_characters
from epistrel.core.catalog import companion as catalog_companion
from epistrel.core.catalog import kinds as catalog_kinds
from epistrel.core.catalog import listing as catalog_listing
from epistrel.core.catalog import personas as catalog_personas
from epistrel.core.catalog import promote as catalog_promote
from epistrel.core.catalog import scenario_models
from epistrel.core.catalog import scenarios as catalog_scenarios
from epistrel.core.catalog import search as catalog_search
from epistrel.core.catalog.report import ValidationResult
from epistrel.core.commit import lifecycle as commitment
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.context import SYSTEM_PRINCIPAL, RequestContext, requesting_principal
from epistrel.core.enums import EntryStatus, StoryStatus, ThunkState
from epistrel.core.erasure import boundary as erasure_boundary
from epistrel.core.erasure import catalog_block
from epistrel.core.erasure import erase as erasure
from epistrel.core.erasure import guard as erasure_guard
from epistrel.core.erasure import ledger as erasure_ledger
from epistrel.core.errors import (
    EpistrelError,
    InvalidRequest,
    LedgerUnavailable,
    ModuleOff,
    PrincipalRequired,
    StoryNotFound,
    SubjectNotFound,
    refuse_nul,
)
from epistrel.core.genesis import create as genesis
from epistrel.core.genesis.models import StoryFromScenario
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
from epistrel.core.media import sweep as media_sweep
from epistrel.core.media import uploads as media_uploads
from epistrel.core.media.poster import FfmpegTools, PosterExtractor
from epistrel.core.media.signing import Signer
from epistrel.core.media.store import BlobStore
from epistrel.core.media.stores import make_store
from epistrel.core.memory import consolidation as consolidation_core
from epistrel.core.memory import pins as memory_pins
from epistrel.core.memory.consolidation import ConsolidateRequest
from epistrel.core.policies.model import Policy, Scope
from epistrel.core.ports import Ports
from epistrel.core.profiles import effective, guarantees, impact
from epistrel.core.profiles import store as profiles
from epistrel.core.profiles import story as story_profile
from epistrel.core.profiles.fingerprint import RuntimeFacts
from epistrel.core.profiles.impact import ImpactPreviewRequest, ImpactReport, TwoStageResult
from epistrel.core.profiles.reservation import safety_minimum
from epistrel.core.profiles.schema import Purposes
from epistrel.core.profiles.validate import ProfileValidation
from epistrel.core.projections import structured as structured_projections
from epistrel.core.propositions import registry as predicate_registry
from epistrel.core.ratings import change as rating_change
from epistrel.core.ratings import reactivation
from epistrel.core.ratings import suggest as rating_suggestion
from epistrel.core.retrieval import lexical_backend
from epistrel.core.retrieval import pipeline as retrieval_pipeline
from epistrel.core.settings_schema import ConfigClass, StorySettings
from epistrel.core.tables import events
from epistrel.core.tables.media import MediaKind, MediaState
from epistrel.core.tables.ownership import EntityType
from epistrel.core.turn_gate import Concurrency, QueueItem
from epistrel.core.verify import shadow as verify_shadow
from epistrel.core.verify import yield_queries
from epistrel.core.viewpoint import (
    is_omniscient,
    require_omniscient,
    resolve_viewpoint,
    with_requested,
)
from epistrel.observability import instrument
from epistrel.orchestrator import queue_drainer
from epistrel.orchestrator.budget import (
    DEFAULT_TURN_DEADLINE_S,
    GUARDRAIL_INPUT,
    GUARDRAIL_OUTPUT,
    TurnBudget,
    effective_max_calls,
)
from epistrel.orchestrator.embeddings.adapter import EMBEDDING as EMBEDDING_PORT
from epistrel.orchestrator.embeddings.adapter import EmbeddingProbe
from epistrel.orchestrator.embeddings.service import EmbeddingUnreachable
from epistrel.orchestrator.jobs import queue as job_queue
from epistrel.orchestrator.jobs.handlers import consolidation as consolidation_job
from epistrel.orchestrator.jobs.handlers import erasure_reconcile as erasure_reconcile_job
from epistrel.orchestrator.jobs.handlers import importance as importance_job
from epistrel.orchestrator.jobs.handlers import lite_summary as lite_summary_job
from epistrel.orchestrator.jobs.handlers import media_sweep as media_sweep_job
from epistrel.orchestrator.jobs.handlers import offboarding as offboarding_job
from epistrel.orchestrator.jobs.handlers import rating_check as rating_check_job
from epistrel.orchestrator.jobs.handlers import reembed as reembed_job
from epistrel.orchestrator.jobs.handlers import turn_queue as turn_queue_job
from epistrel.orchestrator.jobs.handlers.rebuild import (
    REBUILD_KIND,
    RebuildPayload,
    RebuildQueued,
)
from epistrel.orchestrator.jobs.handlers.rebuild import coalesce_key as rebuild_coalesce_key
from epistrel.orchestrator.jobs.handlers.reembed import ReembedRetry
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
from epistrel.orchestrator.models import ModelAccessLayer, ModelError, ModelParams
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
from epistrel.orchestrator.ratings import suggest as rating_suggestion_driver
from epistrel.orchestrator.structured import pipeline as structured_turn
from epistrel.orchestrator.verify import extraction as extraction_job
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
    "consolidate_story": ResultCodec.of(consolidation_job.ConsolidationQueued, status=202),
    # The mutation primitives and the low-level fact calls (#145): story-scoped control writes.
    "quarantine_memory": ResultCodec.of(mutation.MemoryWrite),
    "unquarantine_memory": ResultCodec.of(mutation.MemoryWrite),
    # Author pins (#147): story-scoped control writes.
    "pin_memory": ResultCodec.of(mutation.MemoryWrite),
    "unpin_memory": ResultCodec.of(mutation.MemoryWrite),
    "sanitize_memory": ResultCodec.of(mutation.MemoryWrite),
    "commit_fact": ResultCodec.of(mutation.FactWrite, status=201),
    "supersede_fact": ResultCodec.of(mutation.FactWrite, status=201),
    "lock_fact": ResultCodec.of(mutation.FactWrite),
    "unlock_fact": ResultCodec.of(mutation.FactWrite),
    # A blank story or one from a scenario (201); a scenario creation #109's two stages held
    # back (200, nothing written).
    "create_story": ResultCodec.of(
        stories.CreatedStory | genesis.CreationHeld,
        status=lambda result: 201 if isinstance(result, stories.CreatedStory) else 200,
    ),
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
    # A story's rating (#118): relaxing applies (200); tightening starts its check (202).
    "set_content_rating": ResultCodec.of(
        rating_change.RatingChange,
        status=lambda result: 202 if isinstance(result, rating_change.RatingCheckStarted) else 200,
        revision_field=_CONFIG,
    ),
    "truncate": ResultCodec.of(timeline.Truncation),
    # The edits that make turns active again (#120): 202 when their turns are checked first.
    "untruncate": ResultCodec.of(
        timeline.Untruncation | rating_change.RatingCheckStarted,
        status=lambda result: 202 if isinstance(result, rating_change.RatingCheckStarted) else 200,
    ),
    "swap": ResultCodec.of(
        timeline.Swapped | rating_change.RatingCheckStarted,
        status=lambda result: 202 if isinstance(result, rating_change.RatingCheckStarted) else 200,
    ),
    "fork": ResultCodec.of(
        fork.Forked | rating_change.RatingCheckStarted,
        status=lambda result: 202 if isinstance(result, rating_change.RatingCheckStarted) else 201,
    ),
    "regenerate": ResultCodec.of(RegenerateResult),
    "archive_story": ResultCodec.of(stories.StatusChange),
    # A predicate proposal's decision (#131): approving is a configuration change.
    "approve_predicate": ResultCodec.of(predicate_registry.Decision, revision_field=_CONFIG),
    "reject_predicate": ResultCodec.of(predicate_registry.Decision, revision_field=_CONFIG),
    "unarchive_story": ResultCodec.of(stories.StatusChange),
    "delete_story": ResultCodec.of(stories.DeletedStory, status=204),
    # A story's characters (#114): one added to a running story (story-scoped, like the edits),
    # and a promotion, which only reads the story (deployment-scoped, like a catalog create).
    "add_story_character": ResultCodec.of(stories.StoryCharacterAdded, status=201),
    "promote_character": ResultCodec.of(catalog_promote.Promotion, status=201),
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
    "retry_reembed": ResultCodec.of(ReembedRetry, revision_field=_CONFIG),
    # Gate tightening (#155): deployment-scoped writes of a shape's threshold.
    "set_verify_gate": ResultCodec.of(yield_queries.GateWrite, revision_field=_CONFIG),
    "promote_verify_gate": ResultCodec.of(yield_queries.GateWrite, revision_field=_CONFIG),
    "remove_verify_gate": ResultCodec.of(yield_queries.GateWrite, revision_field=_CONFIG),
    "create_character_draft": ResultCodec.of(
        catalog_characters.DraftWrite, status=201, revision_field=_CONFIG
    ),
    "save_character_draft": ResultCodec.of(catalog_characters.DraftWrite, revision_field=_CONFIG),
    # A character's publish makes a companion version with it (#116), nested in its result.
    "publish_character": ResultCodec.of(
        catalog_characters.CharacterPublished, status=201, revision_field=_CONFIG
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
    # Personas (#114): deployment-scoped like the catalog's writes.
    "create_persona": ResultCodec.of(
        catalog_personas.PersonaWrite, status=201, revision_field=_CONFIG
    ),
    "update_persona": ResultCodec.of(catalog_personas.PersonaWrite, revision_field=_CONFIG),
    "delete_persona": ResultCodec.of(
        catalog_personas.DeletedPersona, status=204, revision_field=_CONFIG
    ),
    # Principal offboarding (#117): stored only once enqueued (202); a dry run or a plan held by
    # its warnings releases its key.
    "offboard_principal": ResultCodec.of(
        offboarding.OffboardingPlan | offboarding.OffboardingQueued,
        status=lambda result: 202 if offboarding.stored(result) else 200,
        revision_field=_CONFIG,
    ),
    # Erasure (#124): deployment-scoped (its key and its ``config_revision``), whichever scope
    # it erases in; a dry run is never stored.
    "erase": ResultCodec.of(erasure.ErasureResult, revision_field=_CONFIG),
    # Feedback export (#126): deployment-scoped; a retry returns the stored answer, no file.
    "export_feedback": ResultCodec.of(
        feedback_export.FeedbackExportWritten, status=201, revision_field=_CONFIG
    ),
    # Engine profiles (#106): deployment-scoped like the catalog's writes.
    "create_profile": ResultCodec.of(profiles.ProfileWrite, status=201, revision_field=_CONFIG),
    # Two-stage (#109): stored only once applied; a report alone releases its key.
    "update_profile": ResultCodec.of(TwoStageResult[profiles.ProfileWrite], revision_field=_CONFIG),
    "copy_profile": ResultCodec.of(profiles.ProfileWrite, status=201, revision_field=_CONFIG),
    "delete_profile": ResultCodec.of(profiles.DeletedProfile, revision_field=_CONFIG),
    # A story's profile and overrides (#113): story-scoped configuration writes, two-stage.
    "assign_engine_profile": ResultCodec.of(
        TwoStageResult[effective.EffectiveConfig], revision_field=_CONFIG
    ),
    "set_profile_overrides": ResultCodec.of(
        TwoStageResult[effective.EffectiveConfig], revision_field=_CONFIG
    ),
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


OffboardingResult = offboarding.OffboardingPlan | offboarding.OffboardingQueued
"""What an offboarding request answers: the plan (nothing written) or the enqueued job (#117)."""


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
_SETTING_FILTERS: dict[config_log.ConfigScope, TypeAdapter[list[str]]] = {
    config_log.ConfigScope.DEPLOYMENT: TypeAdapter(list[config_vocabulary.DeploymentSettingFilter]),
    config_log.ConfigScope.STORY: TypeAdapter(list[config_vocabulary.StorySettingFilter]),
}
"""The ``setting`` vocabulary per scope (#279), the one REST checks the query against (#435)."""


def _policy_items(raw: Any) -> list[StoryPolicyItem]:
    """A settings patch's ``policies`` list (422 ``validation_error`` at its field)."""
    try:
        return _POLICY_ITEMS.validate_python(raw)
    except ValidationError as exc:
        raise InvalidRequest.from_errors(exc.errors(), prefix=("body", "policies")) from None


LIBRARY_ONLY_ATTRIBUTE = "__epistrel_library_only__"


NUL_CHECKED_ATTRIBUTE = "__epistrel_nul_checked__"


def refuses_nul[F: Callable[..., Any]](*own: str) -> Callable[[F], F]:
    """Mark an ``Engine`` read whose arguments are refused when they hold a NUL character
    (#278): PostgreSQL cannot compare text holding one, so a read passing it to SQL failed with a
    database error. ``guard_nul_methods`` wraps every marked read; ``own`` names the arguments
    the read checks itself, under its own fields. Every operation a ``GET`` route serves carries
    it (``tests/test_nul_text.py``)."""

    def mark(method: F) -> F:
        setattr(method, NUL_CHECKED_ATTRIBUTE, own)
        return method

    return mark


def nul_checked_methods(engine_class: type[Engine] | None = None) -> dict[str, tuple[str, ...]]:
    """``{method: own}`` for every ``refuses_nul`` coroutine method of ``engine_class``."""
    cls = engine_class or Engine
    marked: dict[str, tuple[str, ...]] = {}
    for name in dir(cls):
        own = getattr(inspect.getattr_static(cls, name), NUL_CHECKED_ATTRIBUTE, None)
        if isinstance(own, tuple):
            marked[name] = own
    return marked


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
RATING_CHECK_HOOK = "rating_check"
"""The worker recovery hook that ends every rating check whose job ended without ending it
(``change.reconcile``, #118), resuming its story."""


async def _end_rating_checks(conn: AsyncConnection) -> None:
    await rating_change.reconcile(conn, true())


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
        # The embedding jobs and the embedding-model change listener embed through this layer
        # (#138; the last Engine constructed in a process binds them, like the jobs below).
        reembed_job.bind(self.models.embeddings)
        # What Core asks of the Orchestrator (#103): the input gate, policy evaluation, model
        # calls and background jobs. The policy evaluation is the Orchestrator's (#111: the
        # catalog validation's input policies); the other no-op defaults stand until a story
        # wires a real one.
        self.ports = ports or Ports(
            policy_evaluator=CatalogPolicyCheck(self.models),
            # An erasure regenerates the summary it redacted through #72's compression (#124).
            summary_compressor=lite_summary_job.ErasureCompressor(self.models),
        )
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
        # The feedback exports not yet downloaded are rewritten by an erasure in this store
        # (#126; the last Engine constructed in a process binds it).
        feedback_export.bind(self.media)
        # Each story's resolved engine configuration, kept while it holds (#113).
        effective.configure_cache(settings.effective_config_cache)
        # The erasure ledger's file outside the database (FR-ADMIN-7; #125). Until a ``mark``
        # pass has compared it with the database, the first call naming a story runs one, so
        # an Engine used without the app's start-up still refuses a story a restore unerased.
        self.ledger = erasure_ledger.LedgerFile(settings.erasure_ledger_dir)
        # The resurrection guard (FR-ADMIN-9; #127) hashes what a commit writes with the key
        # the ledger's term hashes are keyed with.
        erasure_guard.bind_key(
            None if settings.config_key is None else settings.config_key.get_secret_value()
        )
        self._erasure_marked = False
        self._mark_lock = asyncio.Lock()
        self._reconcile_task: asyncio.Task[None] | None = None

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
        importance_job.bind(self.models)
        consolidation_job.bind(self.models)
        extraction_job.bind(self.models)
        reembed_job.bind(self.models.embeddings)
        rating_check_job.bind(self.models)
        media_sweep_job.bind(self.media)
        erasure_reconcile_job.bind(self._reconcile_run)
        if mode == "api":
            self._recovery = asyncio.create_task(self._recover())
            return
        register_recovery_hook(TURN_QUEUE_HOOK, self._recovery_hook)
        register_recovery_hook(RATING_CHECK_HOOK, _end_rating_checks)
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
            unregister_recovery_hook(RATING_CHECK_HOOK)
        if turn_queue_job.bound() == self.drainer.recover:
            turn_queue_job.bind(None)
        if lite_summary_job.bound() is self.models:
            lite_summary_job.bind(None)
        if consolidation_job.bound() is self.models:
            consolidation_job.bind(None)
        if rating_check_job.bound() is self.models:
            rating_check_job.bind(None)
        if media_sweep_job.bound() is self.media:
            media_sweep_job.bind(None)
        if erasure_reconcile_job.bound() == self._reconcile_run:
            erasure_reconcile_job.bind(None)
        reconcile, self._reconcile_task = self._reconcile_task, None
        if reconcile is not None and not reconcile.done():
            reconcile.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reconcile
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
                await self._replay_gate(
                    replay.stored,
                    None
                    if story_id is not None
                    else catalog_block.key_record(KeyScope.DEPLOYMENT, DEPLOYMENT_SCOPE_ID, key),
                )
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
        """Every mutation carries a requesting principal (FR-AUTH-4), checked by the rule REST
        applies to ``X-Epistrel-Principal`` (#434): 1 to 256 characters without a control
        character, never the reserved ``system``. The spaces and tabs around it are trimmed, and
        the trimmed value is the one recorded."""
        value, problem = requesting_principal(ctx.principal)
        if problem is not None or value is None:
            raise PrincipalRequired(f"the requesting principal {problem}")
        return value

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
        configuration = concurrency.configuration
        if configuration is not None and structured_turn.is_structured(configuration):
            # A structured story's queued turn (#150).
            await self._as_queued(
                item,
                "respond",
                lambda: structured_turn.run_queued(
                    self.db,
                    self.models,
                    ctx,
                    item.story_id,
                    request,
                    operation_id=item.operation_id,
                    principal=item.principal,
                    concurrency=concurrency,
                    planned=item.request.get("turn_budget"),
                    lexical=lexical_backend(self.settings.lexical_backend),
                ),
            )
            return
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

    async def consolidate(
        self, ctx: RequestContext, story_id: str, request: ConsolidateRequest
    ) -> consolidation_job.ConsolidationQueued:
        """Consolidate turns ``from_turn``..``to_turn`` of ``request.scope`` now (FR-MEM-3,10;
        #143): an explicit request, so a window already consolidated is rerun with new
        provenance and its earlier outputs superseded. Handed to a worker as a
        ``consolidation`` job (REST 202 ``{job_id, timeline_revision}``; the same request while
        it waits is that job). 409 ``module_off`` when the story's configuration has
        consolidation off; 422 ``scope_mismatch`` when the window's inputs span scopes no
        output may combine (FR-MEM-9), refused before anything is queued."""
        self.require_principal(ctx)

        async def run(ctx: RequestContext) -> consolidation_job.ConsolidationQueued:
            revision = await self.check_expected_revision(ctx, story_id)
            scope = request.scope or consolidation_core.Scope()
            window = consolidation_core.Window(request.from_turn, request.to_turn)
            async with self.db.begin() as conn:
                dials = await consolidation_core.settings_of(conn, story_id)
                if not dials.enabled:
                    raise ModuleOff(
                        "the story's configuration has consolidation off",
                        {"story_id": story_id, "module": consolidation_core.CONSOLIDATION_MODULE},
                    )
                await consolidation_core.plan(conn, story_id, window, scope)
                job_id = await consolidation_job.enqueue_window(
                    conn, story_id, window, scope, explicit=True
                )
            return consolidation_job.ConsolidationQueued(job_id=job_id, timeline_revision=revision)

        return await self._keyed(
            ctx,
            "consolidate_story",
            run,
            story_id=story_id,
            material={"story_id": story_id, **request.model_dump(mode="json")},
        )

    # -- the mutation primitives and the low-level fact calls (FR-GUARD-4,5; #145) -----------

    async def quarantine_memory(
        self,
        ctx: RequestContext,
        story_id: str,
        memory_id: str,
        request: mutation.QuarantineRequest,
    ) -> mutation.MemoryWrite:
        """Quarantine a memory (FR-GUARD-4; #145): it leaves retrieval and every generation
        context (working memory and history are untouched). A ``control`` operation that bumps
        the revision; a quarantined memory answers unchanged. 404 for an unknown memory, 409
        ``module_off`` when the story keeps no episodic memory."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.MemoryWrite:
            return await mutation.quarantine(
                self.db,
                story_id,
                memory_id,
                request,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "quarantine_memory",
            run,
            story_id=story_id,
            material={"story_id": story_id, "memory_id": memory_id, "body": _body(request)},
        )

    async def unquarantine_memory(
        self, ctx: RequestContext, story_id: str, memory_id: str
    ) -> mutation.MemoryWrite:
        """Reverse a quarantine (#145): the memory is retrievable again (unchanged when it is
        not quarantined)."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.MemoryWrite:
            return await mutation.unquarantine(
                self.db,
                story_id,
                memory_id,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "unquarantine_memory",
            run,
            story_id=story_id,
            material={"story_id": story_id, "memory_id": memory_id},
        )

    async def pin_memory(
        self, ctx: RequestContext, story_id: str, memory_id: str
    ) -> mutation.MemoryWrite:
        """Pin a memory (FR-MEM-8; #147): outside the hot-set budgets and never retired (a
        retired memory is rehydrated to the hot set), in every structured render's context. A
        ``control`` operation that bumps the revision; a pinned memory answers unchanged. 422
        ``pin_limit_exceeded`` past ``max_pinned_per_character`` or ``max_pinned_per_story``
        (the story's text pins count too); 404 for an unknown memory."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.MemoryWrite:
            return await memory_pins.pin(
                self.db,
                story_id,
                memory_id,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "pin_memory",
            run,
            story_id=story_id,
            material={"story_id": story_id, "memory_id": memory_id},
        )

    async def unpin_memory(
        self, ctx: RequestContext, story_id: str, memory_id: str
    ) -> mutation.MemoryWrite:
        """Remove a memory's pin (#147): it counts toward the hot-set budgets again, so an
        unpin above budget opens an overflow episode (unchanged when it is not pinned)."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.MemoryWrite:
            return await memory_pins.unpin(
                self.db,
                story_id,
                memory_id,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "unpin_memory",
            run,
            story_id=story_id,
            material={"story_id": story_id, "memory_id": memory_id},
        )

    @refuses_nul()
    async def hot_set(self, ctx: RequestContext, story_id: str) -> memory_pins.HotSetView:
        """The story's hot set (FR-MEM-8; #147): its counters (recounted now, and as stored),
        budgets, pin ceilings and pins, its open overflow episodes and its latest retirements.
        A read; 404 for an unknown story."""
        return await memory_pins.hot_set_view(self.db, story_id)

    async def sanitize_memory(
        self,
        ctx: RequestContext,
        story_id: str,
        memory_id: str,
        request: mutation.SanitizeRequest,
    ) -> mutation.MemoryWrite:
        """Replace every occurrence of ``span`` in a memory's text by ``surrogate`` (FR-GUARD-4;
        #145): the surrogate passes the input gate, the original stays in an audit event only,
        and the memory is re-indexed and re-embedded. 422 ``span_not_found`` when the span is
        not in the text."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.MemoryWrite:
            return await mutation.sanitize(
                self.db,
                story_id,
                memory_id,
                request,
                principal=principal,
                gate=self._text_gate(ctx, story_id, EntryPoint.SANITIZE),
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "sanitize_memory",
            run,
            story_id=story_id,
            material={"story_id": story_id, "memory_id": memory_id, "body": _body(request)},
        )

    async def commit_fact(
        self, ctx: RequestContext, story_id: str, request: mutation.FactRequest
    ) -> mutation.FactWrite:
        """Commit an author fact (FR-GUARD-4's canonize; #133's ``commit_fact`` with
        ``provenance = authority = author``; #145): #140's check runs first, a conflicting
        functional fact is superseded (``superseded``), an ``uncertain`` verdict is a warning,
        ``locked`` sets ``author_locked``, and an author memory makes it retrievable. A
        position-less ``control`` operation that bumps the revision (201). 409 ``module_off``
        for a Lite story."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.FactWrite:
            return await mutation.commit_fact(
                self.db,
                story_id,
                request,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "commit_fact",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    async def supersede_fact(
        self,
        ctx: RequestContext,
        story_id: str,
        fact_id: str,
        request: mutation.SupersedeRequest,
    ) -> mutation.FactWrite:
        """Supersede a current fact version (#133's ``supersede``; #145): the successor keeps
        the lineage, provenance, authority and lock unless given (201). 404 ``fact_not_found``,
        409 ``fact_superseded``/``fact_inactive``, 422 ``key_mismatch``."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> mutation.FactWrite:
            return await mutation.supersede_fact(
                self.db,
                story_id,
                fact_id,
                request,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "supersede_fact",
            run,
            story_id=story_id,
            material={"story_id": story_id, "fact_id": fact_id, "body": _body(request)},
        )

    async def lock_fact(
        self, ctx: RequestContext, story_id: str, fact_id: str
    ) -> mutation.FactWrite:
        """Lock a current fact version (``author_locked``, FR-VERIFY-11; #145): a supersede of
        the same proposition with the lock set; a locked version answers unchanged."""
        return await self._lock(ctx, story_id, fact_id, locked=True)

    async def unlock_fact(
        self, ctx: RequestContext, story_id: str, fact_id: str
    ) -> mutation.FactWrite:
        """Unlock a current fact version (#145; see ``lock_fact``)."""
        return await self._lock(ctx, story_id, fact_id, locked=False)

    async def _lock(
        self, ctx: RequestContext, story_id: str, fact_id: str, *, locked: bool
    ) -> mutation.FactWrite:
        principal = self.require_principal(ctx)
        name = "lock_fact" if locked else "unlock_fact"

        async def run(ctx: RequestContext) -> mutation.FactWrite:
            return await mutation.set_lock(
                self.db,
                story_id,
                fact_id,
                locked=locked,
                principal=principal,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx, name, run, story_id=story_id, material={"story_id": story_id, "fact_id": fact_id}
        )

    @refuses_nul()
    async def list_facts(
        self,
        ctx: RequestContext,
        story_id: str,
        *,
        viewpoint: str | None = None,
        as_of_story_time: int | None = None,
        as_of_record_time: datetime | None = None,
        subject: str | None = None,
        predicate: str | None = None,
        include_superseded: bool = False,
        limit: int = mutation.DEFAULT_LIST_LIMIT,
        cursor: str | None = None,
    ) -> mutation.FactPage:
        """The story's canon (FR-BITEMP-3; #145), ``omniscient`` only (403
        ``omniscient_required`` otherwise): the current versions (every version with
        ``include_superseded``), true at ``as_of_story_time``, as recorded at
        ``as_of_record_time``, about ``subject`` and of ``predicate`` when given; a page of
        ``limit`` by id, ``cursor`` continuing. 409 ``module_off`` for a Lite story."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        require_omniscient(ctx.viewpoint or "", field="query.viewpoint")
        return await mutation.list_facts(
            self.db,
            story_id,
            as_of_story_time=as_of_story_time,
            as_of_record_time=as_of_record_time,
            subject=subject,
            predicate=predicate,
            include_superseded=include_superseded,
            limit=limit,
            cursor=cursor,
        )

    @library_only("a precondition every story mutation runs (`X-Epistrel-Expected-Revision`)")
    async def check_expected_revision(self, ctx: RequestContext, story_id: str) -> int:
        """Every story-scoped mutation starts here: a stale ``ctx.expected_revision`` is 409
        ``revision_conflict`` with ``current_revision`` before anything is written (FR-RESP-6).

        A start-time precondition only; the commit itself revalidates under FR-CONC-4.
        Deployment-scoped calls never call it, so the header is ignored there.
        """
        return await check_expected_revision(self.db, story_id, ctx.expected_revision)

    @refuses_nul()
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

    @refuses_nul()
    async def get_operation(
        self, ctx: RequestContext, operation_id: str, *, viewpoint: str | None = None
    ) -> operation_view.OperationView:
        """One operation through the history API (FR-CONC-6): kind, status, position, the
        story's revision once it committed, attempts, origin and, within the result window,
        its stored result. A result made for a viewpoint is returned only to a read naming
        that viewpoint (``viewpoint`` or ``ctx.viewpoint``); another named viewpoint is 409
        ``idempotency_conflict``. Unknown is 404. An operation of a story the erasure reconciler
        holds blocked is 503 ``erasure_pending`` like every other read of that story (#333): its
        stored result may carry what the pending erasure removes. So is one whose
        deployment-scoped stored result, or creation report, a pending catalog-scope erasure
        reaches (#346)."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        view = await operation_view.read_operation(self.db, operation_id, viewpoint=ctx.viewpoint)
        if view.story_id is not None:
            await self._erasure_gate(view.story_id)
        if (block := await self._catalog_block()) is not None:
            async with self.db.connect() as conn:
                block.check_records(await catalog_block.operation_records(conn, operation_id))
        return view

    # -- story lifecycle (FR-ADMIN-1, FR-STORE-14,16, FR-CAT-12) ----------------------------

    @overload
    async def create_story(
        self, ctx: RequestContext, spec: stories.StorySpec
    ) -> stories.CreatedStory: ...

    @overload
    async def create_story(
        self, ctx: RequestContext, spec: StoryFromScenario
    ) -> stories.CreatedStory | genesis.CreationHeld: ...

    async def create_story(
        self, ctx: RequestContext, spec: stories.StorySpec | StoryFromScenario
    ) -> stories.CreatedStory | genesis.CreationHeld:
        """Create a story: blank with its cast, rating and settings in one genesis operation, or
        from a scenario (``StoryFromScenario``, FR-SCN-6,9; #119: ``core.genesis.create`` has
        the rules).

        A blank story's every character ``definition`` (and ``deflection_style``, #115) goes
        through the input gate first (FR-GUARD-10), against the global policies and the chosen
        rating's, with the cast's true ages: a violation is 422 ``guardrail_violation`` and no
        story exists. A story from a scenario is refused with nothing written (one 422 with the
        creation report in ``details.report``), held back by #109's two stages
        (``CreationHeld``, the key released), or created in one transaction."""
        principal = self.require_principal(ctx)
        if isinstance(spec, StoryFromScenario):
            request = spec
            # A story copies the catalog entries it is created from, and a catalog-scope erasure
            # never touches a story (FR-STORE-14): none may be reached by a pending one (#346).
            for kind, entry_id in _catalog_entries_of(request):
                await self._catalog_gate(kind, entry_id)
            return await self._keyed(
                ctx,
                "create_story",
                lambda ctx: self._create_from_scenario(ctx, request, principal),
                material={"body": _body(request)},
                keep=lambda result: isinstance(result, stories.CreatedStory),
            )
        stories.validate_spec(spec)
        blank = spec

        async def run(ctx: RequestContext) -> stories.CreatedStory:
            return await self._create_story(ctx, blank, principal)

        created: stories.CreatedStory = await self._keyed(
            ctx, "create_story", run, material={"body": _body(blank)}
        )
        return created

    async def _create_from_scenario(
        self, ctx: RequestContext, request: StoryFromScenario, principal: str
    ) -> stories.CreatedStory | genesis.CreationHeld:
        def cast_of(ages: Sequence[int]) -> list[CastMember]:
            return [
                CastMember(character_id=f"new:{index}", ordinal=index, true_age=age)
                for index, age in enumerate(ages)
            ]

        async def gate(
            texts: Sequence[tuple[str, str]], rating_id: str, ages: Sequence[int]
        ) -> None:
            context = await policy_store.creation_context(self.db, rating_id, cast_of(ages))
            await input_gate.enforce_input(
                self.db,
                self.models,
                context,
                [GateText(locator, text) for locator, text in texts],
                entry_point=EntryPoint.STORY_CREATE,
                principal=principal,
                story_id=None,
            )

        async def rating_check(
            records: Sequence[rating_change.HistoryRecord], rating_id: str, ages: Sequence[int]
        ) -> rating_change.HistoryResult:
            context = await policy_store.creation_context(self.db, rating_id, cast_of(ages))
            settings = await config_log.read_guardrail_settings(self.db)
            return await rating_change.evaluate_history(
                context.active,
                records,
                model_check=rating_check_job.ModelPolicyCheck(self.models, context),
                calls_per_record=settings.rating_check_model_calls_per_record,
            )

        async def write_policies(
            conn: Any, story_id: str, items: Sequence[scenario_models.ScenarioPolicy]
        ) -> None:
            for index, item in enumerate(items):
                await policy_admin.upsert_story_item(
                    conn,
                    story_id,
                    StoryPolicyItem.model_validate(
                        {**item.model_dump(mode="json", exclude_none=True), "scope": "story"}
                    ),
                    principal=principal,
                    field=f"story_settings_defaults.policies[{index}]",
                )

        return await genesis.create_from_scenario(
            self.db,
            request,
            principal=principal,
            gate=gate,
            rating_check=rating_check,
            write_policies=write_policies,
            facts=await self._runtime_facts(),
            ports=self.ports,
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
            GateText(f"characters[{index}].{field}", text)
            for index, c in enumerate(spec.characters)
            for field, text in (
                ("definition", c.definition),
                ("deflection_style", c.deflection_style),
            )
            if text is not None
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
        reserve_output_for: str | Sequence[str] | None = None,
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
            # A gate outside a turn runs under the story's turn deadline (#69; its
            # ``budgets.deadline_ms``, #113).
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

            if await self._plays_structured(story_id):
                # The story's effective configuration runs the structured pipeline (#150).
                return await structured_turn.respond(
                    self.db,
                    self.models,
                    ctx,
                    story_id,
                    request,
                    principal=principal,
                    gate=gate,
                    queue_turn=queue_turn,
                    lexical=lexical_backend(self.settings.lexical_backend),
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

    async def _plays_structured(self, story_id: str) -> bool:
        """Whether ``story_id``'s effective configuration runs the ``structured`` pipeline
        (#129's ``level_at_least``; #113's effective configuration): a turn is routed by it,
        never by a request flag (#150). An unreadable story is ``False``: the Lite path's
        prechecks then answer for it (404, …) exactly as before."""
        try:
            async with self.db.connect() as conn:
                found = await effective.effective_configuration(conn, story_id, validate=False)
        except EpistrelError:
            return False
        return structured_turn.is_structured(found)

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

    @refuses_nul()
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

    # -- commitments (FR-COMMIT-13; #146) -------------------------------------------------------

    @refuses_nul()
    async def inspect_ledger(
        self,
        ctx: RequestContext,
        story_id: str,
        *,
        viewpoint: str | None = None,
        state: ThunkState | None = None,
        discharged: bool | None = None,
        limit: int = commitment.INSPECT_LIMIT_DEFAULT,
        after_id: str | None = None,
    ) -> commitment.LedgerPage:
        """The story's thunks and promissory ledger (docs/02 §9's ``inspect/ledger``; FR-ADMIN-5
        is M5's), ``omniscient`` only (403 ``omniscient_required`` otherwise): a page of
        ``limit`` thunks by id after ``after_id`` (only those in effective ``state`` when
        given), with their ledger entries (``discharged`` filters them). 409 ``module_off`` when
        the story does not run commitment."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        require_omniscient(ctx.viewpoint or "", field="query.viewpoint")
        return await commitment.inspect_ledger(
            self.db,
            story_id,
            state=state,
            discharged=discharged,
            limit=limit,
            after_id=after_id,
        )

    # -- the structured projections (FR-STORE-3, FR-RET-6; #137) ---------------------------------

    @refuses_nul()
    async def scene(
        self,
        ctx: RequestContext,
        story_id: str,
        *,
        as_of: int | None = None,
        viewpoint: str | None = None,
    ) -> structured_projections.SceneView:
        """The scene as of turn ``as_of`` (default the head; 422 beyond it): the player
        character's location and the entities present there, for ``viewpoint`` (or
        ``ctx.viewpoint``, else the story's default output viewpoint; 422
        ``viewpoint_unresolved`` when none resolves). A character viewpoint is not shown an
        entity placed by a fact it may not read."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        async with self.db.connect() as conn:
            return await structured_projections.scene_view(
                conn, story_id, as_of=as_of, viewpoint=ctx.viewpoint
            )

    @refuses_nul()
    async def entity_profile(
        self,
        ctx: RequestContext,
        story_id: str,
        entity_id: str,
        *,
        as_of: int | None = None,
        viewpoint: str | None = None,
    ) -> structured_projections.ProfileView:
        """``entity_id``'s standing profile as of turn ``as_of`` (FR-RET-6): its
        relationships in both directions, where it is and what it holds, for ``viewpoint`` as
        ``scene`` resolves it. 404 ``not_found`` for an entity that is not the story's."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        async with self.db.connect() as conn:
            return await structured_projections.profile_view(
                conn, story_id, entity_id, as_of=as_of, viewpoint=ctx.viewpoint
            )

    # -- retrieval (FR-RET-2..7; #141) ----------------------------------------------------------

    @refuses_nul()
    async def retrieve(
        self,
        ctx: RequestContext,
        story_id: str,
        *,
        q: str,
        viewpoint: str | None = None,
        as_of: int | None = None,
        actor_character_id: str | None = None,
        include_quarantined: bool = False,
    ) -> retrieval_pipeline.RetrievalResult:
        """What a structured render's context would retrieve for ``q`` asked by
        ``actor_character_id`` (default the player) just after turn ``as_of`` (default the
        head), for debugging: the located episodes and assessments, the ranked memories with
        their scores, the profiles and canon, the assembled context and the per-stage timings.
        ``omniscient`` only (403 ``omniscient_required`` for a character viewpoint: it shows
        ranking over private records). A read: no operation, no memory touched. A story whose
        ``episodic`` module is off answers an empty result (``skipped: ["episodic"]``). A
        quarantined memory (#145) is never retrieved unless ``include_quarantined``."""
        ctx = with_requested(ctx, viewpoint, field="query.viewpoint")
        embedder = self.models.embeddings
        async with self.db.connect() as conn:
            return await retrieval_pipeline.read_retrieve(
                conn,
                story_id,
                q=q,
                viewpoint=ctx.viewpoint,
                as_of=as_of,
                actor_character_id=actor_character_id,
                embed=embedder.query_embedder(),
                embed_batch=embedder.batch_embedder(),
                lexical=lexical_backend(self.settings.lexical_backend),
                include_quarantined=include_quarantined,
            )

    # -- the predicate registry (FR-PROP-3,4; #131) ---------------------------------------------

    @library_only("lifecycle: the REST app runs it at start-up")
    async def sync_vocabulary(self) -> int:
        """Re-sync the core vocabulary's rows from ``vocabulary`` (new predicates and doc
        changes reach an existing deployment); answers how many rows changed."""
        return await predicate_registry.sync_core(self.db)

    @refuses_nul()
    async def list_predicates(
        self, ctx: RequestContext, story_id: str
    ) -> predicate_registry.PredicateList:
        """The story's predicate registry: the core vocabulary, its author and auto predicates
        with their pending proposals, and its implication rules."""
        return await predicate_registry.list_predicates(self.db, story_id)

    async def approve_predicate(
        self,
        ctx: RequestContext,
        story_id: str,
        predicate_id: str,
        request: predicate_registry.ApproveRequest,
    ) -> predicate_registry.Decision:
        """Apply the predicate's pending strengthening proposal (or the fields
        ``request.changes`` names): an interpretive configuration change. The answer reports
        the story's propositions the new semantics would flag; none is changed."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> predicate_registry.Decision:
            await self.check_expected_revision(ctx, story_id)
            return await predicate_registry.approve(
                self.db, story_id, predicate_id, request.changes, principal=principal
            )

        return await self._keyed(
            ctx,
            "approve_predicate",
            run,
            story_id=story_id,
            material={"story_id": story_id, "predicate_id": predicate_id, "body": _body(request)},
        )

    async def reject_predicate(
        self, ctx: RequestContext, story_id: str, predicate_id: str
    ) -> predicate_registry.Decision:
        """Close the predicate's pending proposal unapplied."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> predicate_registry.Decision:
            await self.check_expected_revision(ctx, story_id)
            return await predicate_registry.reject(
                self.db, story_id, predicate_id, principal=principal
            )

        return await self._keyed(
            ctx,
            "reject_predicate",
            run,
            story_id=story_id,
            material={"story_id": story_id, "predicate_id": predicate_id},
        )

    @refuses_nul()
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

    @refuses_nul()
    async def story_settings(
        self, ctx: RequestContext, story_id: str
    ) -> tuple[StorySettings, policy_store.StoryGuardrails]:
        """The story's settings (defaults filled in) and its guardrails: effective policies
        with their kind and source, inactive ones, and the model calls they add (FR-GUARD-9)."""
        story = await stories.get_story(self.db, story_id)
        guardrails = await policy_store.story_guardrails(self.db, story_id)
        return StorySettings.from_row(story.settings), guardrails

    @library_only("served as `effective_budget` in `GET`/`PATCH /stories/{id}/settings`")
    @refuses_nul()
    async def effective_budget(self, ctx: RequestContext, story_id: str) -> EffectiveBudget:
        """The turn budget the story's next turn would plan with (#69), from its effective
        configuration's ``budgets`` (#113): the call cap and where it comes from (``story``: its
        overrides, ``profile``: its profile's version, ``deployment``: the deployment's
        ``profile_defaults``, or ``formula`` when the cap is ``null``), the turn deadline, the
        guardrail calls G it reserves (the input check of a turn whose actor has no
        character-scoped input policy) and the safety minimum: ``1 + G`` for Lite, ``1 + B + C +
        G`` for a structured story (#156: its render's extraction and its composed message's
        detection, ``core.profiles.reservation``)."""
        concurrency = await turn_gate.read_concurrency(self.db, story_id)
        guardrails = await policy_store.story_guardrails(self.db, story_id)
        reserved = {
            GUARDRAIL_INPUT: guardrails.calls_input,
            GUARDRAIL_OUTPUT: guardrails.calls_output,
        }
        cap, source = effective_max_calls(
            concurrency.max_model_calls_per_turn, None, sum(reserved.values())
        )
        if concurrency.max_model_calls_per_turn is not None:
            source = concurrency.calls_source
        return EffectiveBudget(
            max_model_calls_per_turn=cap,
            source=source,
            turn_deadline=concurrency.turn_deadline,
            reserved=reserved,
            minimum_required=(
                1 + sum(reserved.values())
                if concurrency.configuration is None
                else safety_minimum(
                    concurrency.configuration.settings,
                    guardrail_input=guardrails.calls_input,
                    guardrail_output=guardrails.calls_output,
                )
            ),
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

    # -- a story's rating (FR-RATE-6,7; #118) ---------------------------------------------------

    async def set_content_rating(
        self, ctx: RequestContext, story_id: str, request: rating_change.RatingChangeRequest
    ) -> rating_change.RatingChange:
        """Change the story's content rating. A target whose effective guardrails are a subset
        of the current rating's (relaxing, FR-RATE-6) applies at once as an interpretive
        configuration change (``RatingApplied``; naming the current rating changes nothing).
        Any other target (tightening, FR-RATE-7) pauses the story and starts a background
        check of its whole active history against the target's effective policies
        (``RatingCheckStarted``, REST 202): read it with ``get_rating_check``; the story
        resumes when it ends, with the target rating only if every record passed. 404, 409
        ``story_archived`` / ``story_paused`` / ``revision_conflict``, 422 ``rating_retired``."""
        principal = self.require_principal(ctx)

        async def start_job(conn: Any) -> str:
            return await job_queue.enqueue(
                conn, rating_check_job.RATING_CHECK_KIND, rating_check_job.RatingCheckPayload()
            )

        async def run(ctx: RequestContext) -> rating_change.RatingChange:
            await self.check_expected_revision(ctx, story_id)
            return await rating_change.change_rating(
                self.db, story_id, request, principal=principal, start_job=start_job
            )

        return await self._keyed(
            ctx,
            "set_content_rating",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    @refuses_nul()
    async def get_rating_check(
        self, ctx: RequestContext, story_id: str, job_id: str
    ) -> rating_change.RatingCheckView:
        """A tightening's check (FR-RATE-7): its status (``checking``, ``applied``,
        ``refused``, ``failed``), progress, who started it and the target; once refused, every
        violation by position and policy (never the text). 404 for an unknown story or a job
        that is not one of its checks."""
        return await rating_change.get_check(self.db, story_id, job_id)

    @refuses_nul()
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
    @refuses_nul()
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

    async def _reactivation_guard(
        self,
        ctx: RequestContext,
        edit: reactivation.Edit,
        story_id: str,
        request: BaseModel,
        principal: str,
    ) -> rating_change.RatingCheckStarted | None:
        """#120's guard at the outer entry of an edit that makes turns active again: ``None``
        when none of them lacks a policy now in effect (the edit runs), otherwise the started
        check (the story is paused, and the job applies the edit if every turn passes)."""

        async def start_job(conn: Any, then: reactivation.Continuation) -> str:
            return await job_queue.enqueue(
                conn,
                rating_check_job.RATING_CHECK_KIND,
                rating_check_job.RatingCheckPayload(then=then),
            )

        return await reactivation.guard(
            self.db,
            reactivation.Continuation(
                operation=edit,
                story_id=story_id,
                args=request.model_dump(mode="json", exclude_none=True),
                principal=principal,
            ),
            start_job=start_job,
            expected_revision=ctx.expected_revision,
        )

    async def untruncate(
        self,
        ctx: RequestContext,
        story_id: str,
        request: timeline.UntruncateRequest | None = None,
    ) -> timeline.Untruncation | rating_change.RatingCheckStarted:
        """Revert the most recent truncation (FR-STORE-18, LIFO): 409 ``nothing_to_undo``
        without one; a swap (#66) when newer turns were written over it. When a turn it would
        reactivate lacks a guardrail policy now in effect (FR-RATE-7, #120), the story pauses
        and those turns are checked first (``RatingCheckStarted``, REST 202: read it with
        ``get_rating_check``); the undo is applied only if they all pass."""
        principal = self.require_principal(ctx)
        request = request or timeline.UntruncateRequest()

        async def run(
            ctx: RequestContext,
        ) -> timeline.Untruncation | rating_change.RatingCheckStarted:
            started = await self._reactivation_guard(
                ctx, reactivation.Edit.UNTRUNCATE, story_id, request, principal
            )
            if started is not None:
                return started
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
    ) -> timeline.Swapped | rating_change.RatingCheckStarted:
        """Make the chain ``request.to_variant_chain`` active and the current continuation
        dormant (FR-STORE-18,28; #66): membership only, reversible by swapping back. Swapping to
        the active chain writes nothing; 409 ``unknown_chain`` for a chain that is not one.
        When a turn it would activate lacks a guardrail policy now in effect (FR-RATE-7, #120),
        those turns are checked first (``RatingCheckStarted``, REST 202), as for
        ``untruncate``."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> timeline.Swapped | rating_change.RatingCheckStarted:
            started = await self._reactivation_guard(
                ctx, reactivation.Edit.SWAP, story_id, request, principal
            )
            if started is not None:
                return started
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
    ) -> fork.Forked | rating_change.RatingCheckStarted:
        """Copy the story's corrected history through ``request.at_turn`` (along
        ``request.variant_chain``) into a new, independent story (FR-STORE-15, FR-CONC-10; #67).
        Keyed per deployment, like story creation; the expected revision is the source's. A
        dormant continuation promoted into the fork whose turns lack a guardrail policy now in
        effect (FR-RATE-7, #120) is checked first (``RatingCheckStarted``, REST 202; the source
        is paused): the fork is created by the check, as its ``outcome``, only if they pass."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> fork.Forked | rating_change.RatingCheckStarted:
            if request.variant_chain is not None:
                started = await self._reactivation_guard(
                    ctx, reactivation.Edit.FORK, story_id, request, principal
                )
                if started is not None:
                    return started
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

    @refuses_nul()
    async def variants(self, ctx: RequestContext, story_id: str) -> list[variants.Chain]:
        """The story's variant chains (``GET /stories/{id}/variants``, #66); a chain that is
        not active says whether making it active would first check its turns
        (``requires_check``, #120)."""
        return await reactivation.chains(self.db, story_id)

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
        generation settings. A structured story's regenerate (#154) reverts what the replaced
        variant committed and reopens the thunks it resolved."""
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
            if await self._plays_structured(story_id):
                # A structured story's regenerate (#154): the commit boundary reverts what the
                # replaced variant committed.
                return await structured_turn.regenerate(
                    self.db,
                    self.models,
                    ctx,
                    story_id,
                    request,
                    principal=principal,
                    gate=gate,
                    lexical=lexical_backend(self.settings.lexical_backend),
                )
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

    @refuses_nul()
    async def list_stories(
        self,
        ctx: RequestContext,
        *,
        status: StoryStatus | None = None,
        owner: str | None = None,
        relation: str | Sequence[str] = (),
    ) -> list[stories.StoryView]:
        """Every story, newest first, unless the request filters: by ``status``, by ``owner``
        (a principal), or by ``relation`` (each ``name:principal``, or ``name:`` for any
        principal; repeated filters all hold). Owners and relations narrow a list only when
        asked (FR-AUTH-8). A story the erasure reconciler holds blocked is listed with its
        status but without its name and settings until the reapply settles it (#348): every
        call naming it is refused, and the list must not serve what the erasure removes."""
        if not self._erasure_marked:
            await self._mark_erasures()
        found = await stories.list_stories(
            self.db,
            status=status,
            owner=ownership.owner_filter(owner),
            relations=[ownership.parse_relation_filter(value) for value in relation],
        )
        return [
            dataclasses.replace(story, name=None, settings={})
            if story.status is StoryStatus.BLOCKED_BY_ERASURE
            else story
            for story in found
        ]

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
            # The core check, not the gated method: a story blocked by the erasure reconciler
            # may still be deleted (#125).
            await check_expected_revision(self.db, story_id, ctx.expected_revision)
            removed = await stories.delete_story(self.db, story_id, principal=principal)
            return stories.DeletedStory(removed)

        return await self._keyed(ctx, "delete_story", run, material={"story_id": story_id})

    # -- a story's characters: ad hoc and promoted (FR-CAT-7,8; #114) ----------------------------

    async def add_story_character(
        self, ctx: RequestContext, story_id: str, request: stories.AdHocCharacter
    ) -> stories.StoryCharacterAdded:
        """Add a character to a running story (FR-CAT-7): story-scoped, never a catalog entry,
        never a player, with its required true age (FR-CAT-12). 404 for an unknown story; 422
        for a full cast (``CAST_MAX``), a name the story has, or an image that is not a live
        image asset; 409 ``story_archived``; 409 ``revision_conflict`` for a stale expected
        revision; 423 ``story_busy`` while a turn is in flight; then the definition passes the
        input gate, checked with the new character's age in the cast (422
        ``guardrail_violation``). The addition bumps the revision (FR-CONC-1), as a
        story-text edit does: the cast is part of every Lite prompt."""
        principal = self.require_principal(ctx)

        async def gate(texts: Sequence[tuple[str, str]]) -> None:
            context = await policy_store.guard_context(self.db, story_id)
            joining = CastMember(
                character_id="new:0",
                ordinal=max((m.ordinal for m in context.cast), default=-1) + 1,
                true_age=request.true_age,
            )
            context = dataclasses.replace(context, cast=(*context.cast, joining))
            deadline_s = (await turn_gate.read_concurrency(self.db, story_id)).turn_deadline
            await input_gate.enforce_input(
                self.db,
                self.models,
                context,
                [GateText(locator, text) for locator, text in texts],
                entry_point=EntryPoint.STORY_CHARACTER,
                principal=principal,
                story_id=story_id,
                deadline_s=deadline_s,
            )

        async def run(ctx: RequestContext) -> stories.StoryCharacterAdded:
            return await stories.add_story_character(
                self.db,
                story_id,
                request,
                principal=principal,
                gate=gate,
                idempotency_key=ctx.idempotency_key,
                expected_revision=ctx.expected_revision,
            )

        return await self._keyed(
            ctx,
            "add_story_character",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
        )

    async def promote_character(
        self,
        ctx: RequestContext,
        story_id: str,
        character_id: str,
        request: catalog_promote.PromoteRequest | None = None,
    ) -> catalog_promote.Promotion:
        """Promote a story character to a new **draft** catalog entry (FR-CAT-8): only its
        definition fields are copied (true age, its free-text definition as
        ``background_flavor``, its safe fallback line, its images by reference), never a story
        event, pin, summary or fact; the story character keeps a link to the entry in
        ``promoted_to`` and nothing else changes. Owned by ``request.owner`` or else the
        requesting principal, rated ``request.content_rating_id`` or else as the story. Keyed per
        deployment; the expected revision, when sent, is checked first; allowed on an archived
        story and while a turn is in flight. 404 for an unknown story or a character not in it;
        422 for a player character or a rating that cannot be assigned."""
        principal = self.require_principal(ctx)
        request = request or catalog_promote.PromoteRequest()

        async def run(ctx: RequestContext) -> catalog_promote.Promotion:
            await self.check_expected_revision(ctx, story_id)
            return await catalog_promote.promote(
                self.db, story_id, character_id, request, principal=principal
            )

        return await self._keyed(
            ctx,
            "promote_character",
            run,
            material={
                "story_id": story_id,
                "character_id": character_id,
                "body": _body(request),
            },
        )

    # -- content ratings and deployment settings (FR-RATE-1,2; deployment-scoped) --------------

    @refuses_nul()
    async def list_content_ratings(
        self, ctx: RequestContext, *, include_retired: bool = False
    ) -> list[ratings.Rating]:
        return await ratings.list_ratings(self.db, include_retired=include_retired)

    @refuses_nul()
    async def get_content_rating(self, ctx: RequestContext, rating_id: str) -> ratings.RatingDetail:
        return await ratings.get_rating(self.db, rating_id)

    @refuses_nul()
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

    @refuses_nul()
    async def deployment_settings(self, ctx: RequestContext) -> deployment.DeploymentState:
        return await deployment.read_settings(self.db)

    async def update_deployment_settings(
        self, ctx: RequestContext, patch: deployment.SettingsPatch
    ) -> deployment.DeploymentState:
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "update_deployment_settings",
            lambda _: deployment.update_settings(
                self.db, patch, principal=principal, purposes=self.purposes
            ),
            material={"body": patch.model_dump(mode="json", exclude_unset=True)},
        )

    @refuses_nul()
    async def config_changes(
        self,
        ctx: RequestContext,
        *,
        scope: config_log.ConfigScope,
        story_id: str | None = None,
        settings: str | Sequence[str] = (),
        change_class: ConfigClass | None = None,
        since: datetime | None = None,
        after: str | None = None,
        limit: int = config_log.DEFAULT_PAGE,
    ) -> config_log.ConfigChangePage:
        """A page of the configuration log (FR-CONC-14, #64): the deployment's changes, or a
        story's (404 for an unknown story), newest first. Each ``settings`` name is checked
        against the scope's closed vocabulary, as REST checks ``setting`` (#279, #435): an
        unknown name is 422 ``validation_error`` at ``query.setting``, never an empty page. A
        deployment change whose values a pending catalog-scope erasure reaches is left out until
        it has been reapplied (#429, as #346's lists)."""
        try:
            _SETTING_FILTERS[scope].validate_python(list(settings))
        except ValidationError as exc:
            raise InvalidRequest.from_errors(exc.errors(), prefix=("query", "setting")) from None
        if story_id is not None:
            await check_expected_revision(self.db, story_id, None)
        block = await self._catalog_block()
        return await config_log.list_config_changes(
            self.db,
            scope=scope,
            scope_id=story_id,
            settings=settings,
            change_class=change_class,
            since=since,
            after=after,
            limit=limit,
            excluded=() if block is None else block.records(catalog_block.CONFIG_CHANGES),
        )

    # -- the policy store (FR-GUARD-1,8,9; NFR-SEC-1) ----------------------------------------
    # The writes below are the only callers of ``orchestrator.policies.admin``: the operator
    # surface (``/admin/policies``) and story settings (#63). Nothing on a turn's path calls them.

    @refuses_nul()
    async def policies_list(
        self,
        ctx: RequestContext,
        *,
        scope: Scope | None = None,
        story_id: str | None = None,
        include_retired: bool = False,
    ) -> list[Policy]:
        """Policies ordered by scope, scope id and name. The story and character policies of a
        story the erasure reconciler holds blocked are left out (they may still name the
        subject); the ledger is compared first, as for ``list_stories`` (#363). So is a policy
        whose text a pending catalog-scope erasure reaches, until it has been reapplied (#429)."""
        if not self._erasure_marked:
            await self._mark_erasures()
        listed = await policy_store.list_policies(
            self.db,
            scope=scope,
            story_id=story_id,
            include_retired=include_retired,
            exclude_blocked=True,
        )
        if (block := await self._catalog_block()) is None:
            return listed
        return [p for p in listed if not block.hides_record(catalog_block.POLICIES, p.id)]

    @refuses_nul()
    async def policies_get(self, ctx: RequestContext, policy_id: str) -> Policy:
        """One policy, retired or not. A policy of a blocked story is 503 ``erasure_pending``,
        as every call naming the story is (#363), and so is one whose text a pending
        catalog-scope erasure reaches (#429)."""
        if not self._erasure_marked:
            await self._mark_erasures()
        policy = await policy_store.get_policy(self.db, policy_id)
        if policy.story_id is not None:
            await erasure_ledger.check_story(self.db, policy.story_id)
        await self._policy_gate(policy_id)
        return policy

    @library_only("served as `effective_policies` in `GET /stories/{id}/settings`")
    @refuses_nul()
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
        """Replace a global or rating policy's content. 503 ``erasure_pending`` while a pending
        catalog-scope erasure reaches its text (#429): the change would record it again."""
        principal = self.require_principal(ctx)
        await self._policy_gate(policy_id)
        return await self._keyed(
            ctx,
            "policies_update",
            lambda _: policy_admin.update_policy(self.db, policy_id, fields, principal=principal),
            material={"policy_id": policy_id, "body": _body(fields)},
        )

    async def policies_retire(
        self, ctx: RequestContext, policy_id: str
    ) -> policy_admin.PolicyWrite:
        """Retire a global or rating policy. 503 ``erasure_pending`` while a pending
        catalog-scope erasure reaches its text (#429): the result carries it."""
        principal = self.require_principal(ctx)
        await self._policy_gate(policy_id)
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

    @refuses_nul()
    async def model_configuration(
        self, ctx: RequestContext
    ) -> tuple[int, dict[str, model_config.ConfigEntry]]:
        """``config_revision`` and every configurable purpose, ``default`` included."""
        return await self.models.configuration()

    @refuses_nul()
    async def model_config_entry(
        self, ctx: RequestContext, purpose: str
    ) -> tuple[int, model_config.ConfigEntry]:
        return await model_config.entry(self.db, self.models.keybox, purpose)

    async def replace_model_config(
        self, ctx: RequestContext, purpose: str, body: model_config.ConfigReplacement
    ) -> model_config.ConfigWrite:
        """Replace a purpose's own configuration. For the ``embedding`` port (#138) a new
        model is probed first (422 ``embedding_unreachable`` when it does not answer): the
        write then moves the embedding pin and enqueues the re-embed jobs in its transaction
        (``reembed_job.on_embedding_model_changed``)."""
        principal = self.require_principal(ctx)
        probe = await self._probe_embedding_change(purpose, body)
        with reembed_job.changing(self.models.embeddings, probe):
            return await self._keyed(
                ctx,
                "replace_model_config",
                lambda _: model_config.replace_config(
                    self.db, purpose, body, keybox=self.models.keybox, principal=principal
                ),
                material={"purpose": purpose, "body": _body(body)},
            )

    async def _probe_embedding_change(
        self, purpose: str, body: model_config.ConfigReplacement
    ) -> EmbeddingProbe | None:
        """The probe of the embedding model ``body`` names when it is a different one; ``None``
        for any other purpose, an unset model or the same model."""
        if purpose != EMBEDDING_PORT or body.model is None:
            return None
        if body.params is not None:
            model_config.check_port_params(purpose, body.params)
        _, rows = await model_config.snapshot(self.db)
        own = rows.get(purpose)
        if own is not None and own.model == body.model:
            return None
        candidate = model_config.EffectiveConfig(
            purpose=purpose,
            base_url=body.base_url,
            model=body.model,
            api_key_ciphertext=None if own is None else own.api_key_ciphertext,
            params=body.params or ModelParams(),
            revision=0,
        )
        try:
            return await self.models.embeddings.probe(candidate)
        except ModelError as exc:
            raise EmbeddingUnreachable(str(exc.details.get("reason", "unknown"))) from None

    # -- claim detection's shadow sample (FR-OBS-4, FR-VERIFY-13; #148) -----------------------

    @refuses_nul()
    async def extraction_shadow_report(
        self,
        ctx: RequestContext,
        *,
        window_from: datetime | None = None,
        window_to: datetime | None = None,
        story_id: str | None = None,
        limit: int = 50,
        after: str | None = None,
    ) -> verify_shadow.ShadowReport:
        """What ``gated`` claim extraction missed over a window (30 days by default; one story
        or every one), with detection and checking counted separately, and one page of the
        samples (by id, after ``after``)."""
        del ctx
        async with self.db.connect() as conn:
            return await verify_shadow.report(
                conn,
                window_from=window_from,
                window_to=window_to,
                story_id=story_id,
                limit=limit,
                after=after,
            )

    # -- escalation yield and gate tightening (FR-OBS-2,4, NFR-PERF-5; #155) -------------------

    @refuses_nul()
    async def escalation_yield(
        self,
        ctx: RequestContext,
        *,
        window_from: datetime | None = None,
        window_to: datetime | None = None,
    ) -> yield_queries.YieldReport:
        """Escalation yield over a window (the last 30 days by default): per gate reason, per
        predicate shape and per both, the escalations, flips and rates, and the overall
        escalation rate with its reference baseline."""
        del ctx
        async with self.db.connect() as conn:
            return await yield_queries.yield_report(
                conn, window_from=window_from, window_to=window_to
            )

    async def set_verify_gate(
        self, ctx: RequestContext, request: yield_queries.GateRequest
    ) -> yield_queries.GateWrite:
        """Propose a shape's gate threshold in ``shadow``, or put it ``live`` (confirmed with
        ``ignore_warnings``; without it nothing is written and the key is released)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "set_verify_gate",
            lambda _: yield_queries.set_gate(self.db, request, actor=principal),
            material=request.model_dump(mode="json"),
            keep=lambda written: written.applied,
        )

    async def promote_verify_gate(self, ctx: RequestContext, shape: str) -> yield_queries.GateWrite:
        """Promote a shape's shadowed threshold (409 ``gate_not_promotable`` below the rule)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "promote_verify_gate",
            lambda _: yield_queries.promote_gate(self.db, shape, actor=principal),
            material={"shape": shape},
        )

    async def remove_verify_gate(self, ctx: RequestContext, shape: str) -> yield_queries.GateWrite:
        """Remove a shape's threshold: proposed or live, the shape is not tightened any more."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "remove_verify_gate",
            lambda _: yield_queries.remove_gate(self.db, shape, actor=principal),
            material={"shape": shape},
        )

    # -- the re-embed run (FR-EMBED-3,4; #138) ------------------------------------------------

    @refuses_nul()
    async def reembed_status(
        self, ctx: RequestContext, *, limit: int = 50, after: str | None = None
    ) -> reembed_job.ReembedStatus:
        """The last embedding-model change's re-embed run: its versions, start and finish, and
        one page of stories (by id, after ``after``) with their job's status and progress."""
        async with self.db.connect() as conn:
            return await reembed_job.status(conn, limit=limit, after=after)

    async def retry_reembed(self, ctx: RequestContext) -> ReembedRetry:
        """Enqueue a new re-embed job for every story whose latest one failed."""
        self.require_principal(ctx)

        async def run(_: RequestContext) -> ReembedRetry:
            async with self.db.begin() as conn:
                retried = await reembed_job.retry(conn)
                revision = await model_config.current_revision(conn)
            return ReembedRetry(retried=retried, config_revision=revision)

        return await self._keyed(ctx, "retry_reembed", run, material={})

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
        *,
        with_companion: bool = False,
    ) -> catalog_characters.CharacterPublished:
        """Publish the draft as an immutable version ``n + 1`` (FR-CAT-3), compared on
        ``request.draft_revision`` like a save; 422 ``publish_invalid`` with the report when the
        draft cannot be published. The draft stays equal to what was published, at the next
        revision. In the same transaction (FR-CAT-10; #116) a companion version is made, pinned
        to version ``n + 1``: a copy of the latest published companion content (the companion's
        defaults on the first publish), leaving an unpublished companion draft unpublished with
        its pin moved; or, with ``with_companion``, the companion's draft, compared on
        ``request.companion_draft_revision`` (a ``CharacterPublishRequest``; required then and
        refused otherwise, 422 before the ``Idempotency-Key`` is claimed). A companion problem
        refuses the whole publish (``details.entity`` names the side) and nothing is written.
        The answer nests the companion version (``companion``)."""
        principal = self.require_principal(ctx)
        catalog_companion.companion_revision(request, with_companion)
        return await self._keyed(
            ctx,
            "publish_character",
            lambda _: catalog_characters.publish(
                self.db,
                character_id,
                request,
                principal=principal,
                ports=self.ports,
                with_companion=with_companion,
            ),
            material={
                "character_id": character_id,
                "body": _body(request),
                "with_companion": with_companion,
            },
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

    @refuses_nul()
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

    @refuses_nul()
    async def list_character_versions(
        self, ctx: RequestContext, character_id: str
    ) -> list[catalog_characters.VersionSummary]:
        """Every published version of the entry, oldest first: number, time and publisher."""
        return await catalog_characters.versions(self.db, character_id)

    @refuses_nul()
    async def get_companion(
        self,
        ctx: RequestContext,
        character_id: str,
        *,
        version: int | None = None,
        draft: bool = False,
        projection: catalog_characters.Projection = catalog_characters.Projection.CARD,
    ) -> scenario_models.ScenarioCard | scenario_models.ScenarioFull:
        """The character's companion scenario (FR-CAT-5; #116), as ``get_scenario`` reads it:
        its latest published version (``version``: that one; ``draft``: the draft), and its
        draft while it was never published. 404 for an unknown character or version."""
        return await catalog_characters.get_companion(
            self.db, character_id, version=version, draft=draft, projection=projection
        )

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

    @refuses_nul()
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

    @refuses_nul()
    async def list_scenario_versions(
        self, ctx: RequestContext, scenario_id: str
    ) -> list[catalog_scenarios.VersionSummary]:
        """Every published version of the scenario, oldest first: number, time and publisher."""
        return await catalog_scenarios.versions(self.db, scenario_id)

    # -- the rating suggestion (FR-RATE-8; #121) --------------------------------------------------
    # Advisory: nothing reads its table (the import-linter contract
    # ``rating-suggestion-is-advisory``), and it writes nothing.

    async def suggest_ratings(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        entry_id: str,
        request: rating_suggestion.SuggestionRequest | None = None,
        *,
        include_retired: bool = False,
    ) -> rating_suggestion.RatingSuggestion:
        """Suggest ratings for a catalog character or scenario (FR-RATE-8): a table with one row
        for every rating (``include_retired``: the retired ones too), each ``appropriate`` or not
        and, for a no, every concern: the policy or description clause broken and the element
        that breaks it. ``request.version``: a published version or ``"draft"``; omitted, the
        latest published version, else the draft. Deterministic policies are evaluated
        directly, the rest through one ``rating_suggestion`` call per rating (a failed call
        leaves its row ``error`` set). Advisory (``advisory: true``): it changes no rating,
        blocks nothing, and nothing reads it. It writes nothing, so it takes no
        ``Idempotency-Key``. Its concerns quote private fields: an author-facing read. 404 for
        an unknown entry or version."""
        service = catalog_kinds.service_of(entity_type)
        kind: rating_suggestion.EntryType = (
            "character" if service.entity_type is EntityType.CHARACTER else "scenario"
        )
        version = None if request is None else request.version
        started = ctx.received_at if ctx.received_at is not None else time.monotonic()
        with model_metrics.recording() as calls:
            table = await rating_suggestion_driver.suggest(
                self.db,
                self.models,
                kind,
                entry_id,
                version,
                include_retired=include_retired,
            )
        return table.model_copy(
            update={
                "model_calls": [call.model_dump(mode="json") for call in calls.calls],
                "elapsed_ms": round((time.monotonic() - started) * 1000),
            }
        )

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

    @refuses_nul("q", "label", "content_rating", "listing_status", "owner", "relation")
    async def search_catalog(
        self,
        ctx: RequestContext,
        entity_type: EntityType | str,
        *,
        q: str | None = None,
        label: str | Sequence[str] = (),
        content_rating: str | None = None,
        listing_status: str | None = None,
        owner: str | None = None,
        relation: str | Sequence[str] = (),
        status: EntryStatus | str | None = None,
        include_companions: bool = False,
        limit: int = catalog_search.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> catalog_search.CatalogPage:
        """A page of catalog characters or scenarios as cards (FR-API-6): the ``published``
        ones (``status``: the ``draft`` or ``archived`` ones), newest first, every owner and
        listing status included. ``q`` searches the latest published version's public card
        (best match first, with each card's ``rank``); ``label`` (one, or several that must all
        hold), ``content_rating``, ``listing_status``, ``owner`` and ``relation``
        (``name:principal``, or ``name:`` for any principal; one, or several that must all
        hold) narrow the list only when given (FR-AUTH-8). A plain string ``label`` or
        ``relation`` is one value, as one query parameter is. Companion scenarios are listed
        only with ``include_companions``. ``cursor`` (a page's ``next_cursor``) continues with
        the same filters. An entry a pending catalog-scope erasure reaches is left out until it
        has been reapplied (#346), so a page may hold fewer than ``limit``."""
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
        page = await catalog_search.search(self.db, service, query, limit=limit, cursor=cursor)
        kind = (
            catalog_block.CHARACTER
            if service.entity_type is EntityType.CHARACTER
            else catalog_block.SCENARIO
        )
        if (block := await self._catalog_block()) is None:
            return page
        return dataclasses.replace(
            page, items=[i for i in page.items if not block.hides(kind, str(i.id))]
        )

    # -- personas (FR-CAT-6, FR-AUTH-5; FR-API-6; #114) -----------------------------------------
    # Deployment-scoped like the catalog: each write is one transaction recording a story-less
    # ``control`` operation, keyed per deployment. Nothing here reads or changes a story: a story
    # keeps the snapshot of the persona it was created with.

    async def create_persona(
        self, ctx: RequestContext, request: catalog_personas.PersonaCreate
    ) -> catalog_personas.PersonaWrite:
        """Save a persona (FR-CAT-6) owned by ``request.owner`` or else the requesting principal
        (FR-AUTH-5); 409 ``persona_name_taken`` when the owner has one of that name (ignoring
        case)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "create_persona",
            lambda _: catalog_personas.create(self.db, request, principal=principal),
            material={"body": _body(request)},
        )

    @refuses_nul()
    async def get_persona(self, ctx: RequestContext, persona_id: str) -> catalog_personas.Persona:
        """The persona; 404 for an unknown one."""
        return await catalog_personas.get(self.db, persona_id)

    @refuses_nul("owner", "relation", "cursor")
    async def list_personas(
        self,
        ctx: RequestContext,
        *,
        owner: str | None = None,
        relation: str | Sequence[str] = (),
        limit: int = catalog_personas.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> catalog_personas.PersonaPage:
        """A page of personas, newest first: every principal's unless ``owner`` (a principal)
        or ``relation`` (``name:principal``, or ``name:`` for any principal; one, or several that
        must all hold) narrow it (FR-AUTH-8). A plain string ``relation`` is one value.
        ``cursor`` (a page's ``next_cursor``) continues with the same filters. A persona a
        pending catalog-scope erasure reaches is left out until it has been reapplied (#346)."""
        values = (relation,) if isinstance(relation, str) else tuple(relation)
        refuse_nul({"owner": owner, "relation": list(values), "cursor": cursor}, ("query",))
        page = await catalog_personas.list_personas(
            self.db,
            owner=ownership.owner_filter(owner),
            relations=[
                ownership.parse_relation_filter(value, field=f"query.relation[{index}]")
                for index, value in enumerate(values)
            ],
            limit=limit,
            cursor=cursor,
        )
        if (block := await self._catalog_block()) is None:
            return page
        return dataclasses.replace(
            page, items=[p for p in page.items if not block.hides(catalog_block.PERSONA, p.id)]
        )

    async def update_persona(
        self, ctx: RequestContext, persona_id: str, patch: catalog_personas.PersonaPatch
    ) -> catalog_personas.PersonaWrite:
        """Edit the persona with a JSON merge patch, last write wins: ``null`` removes
        ``gender`` and is 422 for a required field; 409 ``persona_name_taken`` for a name its
        owner already uses. An edit that changes nothing records nothing. Stories created with
        the persona keep their snapshot (FR-CAT-6)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "update_persona",
            lambda _: catalog_personas.update(self.db, persona_id, patch, principal=principal),
            material={"persona_id": persona_id, "body": patch.document()},
        )

    async def delete_persona(
        self, ctx: RequestContext, persona_id: str
    ) -> catalog_personas.DeletedPersona:
        """Delete the persona (a hard delete) with its relations, recorded with a
        ``persona_deleted`` audit row; stories created with it keep their snapshot
        (FR-CAT-6)."""
        principal = self.require_principal(ctx)
        return await self._keyed(
            ctx,
            "delete_persona",
            lambda _: catalog_personas.delete(self.db, persona_id, principal=principal),
            material={"persona_id": persona_id},
        )

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

        async def transfer(principal: str) -> ownership.OwnerTransfer:
            # A persona's name is unique per owner (#114): a transfer that would give the new
            # owner two of one name is 409 ``persona_name_taken``.
            with catalog_personas.name_conflicts(
                request.principal, field="body.principal", persona_id=entity_id
            ):
                return await ownership.transfer_owner(
                    self.db, kind, entity_id, request.principal, principal=principal
                )

        return await self._ownership_write(
            ctx, "transfer_owner", kind, entity_id, transfer, {"body": _body(request)}
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

    @refuses_nul()
    async def list_relations(
        self, ctx: RequestContext, entity_type: EntityType | str, entity_id: str
    ) -> ownership.Relations:
        """The entity's owner and its relations, by relation name, then principal."""
        return await ownership.list_relations(
            self.db, ownership.entity_type_of(entity_type), entity_id
        )

    # -- principals: lookup and offboarding (FR-AUTH-7; #117) ----------------------------------
    # A principal is opaque and never resolved: a lookup of one nothing names is empty. The
    # offboarding request is deployment-scoped and keyed per deployment; its job runs as
    # ``system``.

    @refuses_nul("principal_id", "cursors")
    async def lookup_principal(
        self,
        ctx: RequestContext,
        principal_id: str,
        *,
        limit: int = principals.DEFAULT_LIMIT,
        cursors: Mapping[str, str] | None = None,
    ) -> principals.PrincipalLookup:
        """Every entity linked to ``principal_id`` (FR-AUTH-7), by type: what it owns (a
        character with its companion; companions are not listed as scenarios) and what names it
        in a relation (with the relations' names), each type a page of up to ``limit`` with its
        count, newest first. ``cursors`` maps a page (``stories``, ``related.stories``, …) to its
        previous page's ``next_cursor``. An unknown principal answers empty pages. A story the
        erasure reconciler holds blocked is listed without its name (#359); the ledger is
        compared first, as for ``list_stories`` (#360). So is a catalog entry, persona or
        media asset a pending catalog-scope erasure reaches (#346)."""
        found = principals.lookup_principal_id(principal_id)
        given = dict(cursors or {})
        refuse_nul({"principal": found, "cursors": given}, ("query",))
        if not self._erasure_marked:
            await self._mark_erasures()
        lookup = await principals.lookup(self.db, found, limit=limit, cursors=given)
        if (block := await self._catalog_block()) is None:
            return lookup
        return principals.without_names(lookup, block.hides)

    async def offboard_principal(
        self, ctx: RequestContext, request: offboarding.OffboardingRequest
    ) -> OffboardingResult:
        """Offboard ``request.principal`` (FR-AUTH-7): apply each type's action (``delete``,
        ``transfer`` to ``request.transfer_to``, or ``keep``; relations ``remove`` or ``keep``)
        to a snapshot of what it owns and is related to, taken now. A dry run, or a request
        whose plan warns (a shared story, a profile or media asset others use, the default
        profile) without ``ignore_warnings``, answers the plan (``applied`` false), writes
        nothing and releases its key. Otherwise the offboarding is enqueued as one background
        job (``OffboardingQueued``, REST 202) under the request's ``Idempotency-Key``: a retry
        answers the same job. 422 ``principal_reserved``, ``transfer_target_required`` or
        ``transfer_target_invalid`` for a request that cannot run; 409
        ``offboarding_in_progress`` while the principal has one queued or running."""
        actor = self.require_principal(ctx)
        offboarding.check_request(request)

        async def run(
            _: RequestContext,
        ) -> offboarding.OffboardingPlan | offboarding.OffboardingQueued:
            async with self.db.begin() as conn:
                await offboarding.lock_principal(conn, request.principal)
                await offboarding.refuse_active(conn, request.principal)
                planned = await offboarding.plan(conn, request)
                revision = await offboarding.config_revision(conn)
                if request.dry_run or (planned.warnings and not request.ignore_warnings):
                    return offboarding.OffboardingPlan(
                        counts=planned.counts,
                        warnings=planned.warnings,
                        config_revision=revision,
                    )
                job_id = await job_queue.enqueue(
                    conn, offboarding_job.OFFBOARDING_KIND, offboarding_job.OffboardingPayload()
                )
                operation_id = await control_ops.record_control_operation(conn, actor=actor)
                await offboarding.create(
                    conn,
                    job_id,
                    request,
                    planned,
                    created_by=actor,
                    operation_id=operation_id,
                )
                return offboarding.OffboardingQueued(
                    job_id=job_id,
                    counts=planned.counts,
                    warnings=planned.warnings,
                    config_revision=revision,
                )

        return await self._keyed(
            ctx,
            "offboard_principal",
            run,
            material={"body": request.model_dump(mode="json")},
            keep=offboarding.stored,
        )

    @refuses_nul()
    async def get_offboarding(
        self, ctx: RequestContext, job_id: str
    ) -> offboarding.OffboardingView:
        """An offboarding's status, progress and request, and once it ends its report: every
        entity deleted, transferred, removed, kept or skipped, and what is still linked to the
        principal (FR-AUTH-7). 404 for an unknown job (or a purged report)."""
        return await offboarding.get(self.db, job_id)

    # -- erasure (FR-ADMIN-2,6, NFR-PRIV-2; #124) -------------------------------------------------

    async def erase(
        self, ctx: RequestContext, request: erasure.ErasureRequest
    ) -> erasure.ErasureResult:
        """Erase a subject (a story ``character``, a ``catalog_character`` or a ``persona``)
        within one story or from the catalog, so that afterwards no text in that scope names
        it (``core.erasure.erase``). A ``dry_run`` reports what would be erased and writes
        nothing (no operation, no key; zero counts for an unknown subject). Otherwise the
        erasure runs under the request's ``Idempotency-Key`` (deployment-scoped; ``dry_run`` is
        in the request hash): a story-scope erasure as one ``control`` operation that bumps the
        story's revision (its regenerated summary and rebuilt projections follow), a
        catalog-scope one as one deployment operation that never touches a story. Certain
        mentions are erased; candidates (a common word of the name capitalised at the start of
        a sentence, ADR-154) only when ``confirm_candidates`` names them, by the ids a dry run
        lists. 422 ``invalid_erasure_scope`` for a story character scoped to the catalog, and
        ``unknown_erasure_candidate`` for a confirmed id the erasure does not meet; 503
        ``erasure_pending`` for a story the erasure reconciler holds blocked, dry run or not
        (#383), and for a catalog-scope request while a catalog-scope erasure is pending
        (#346); 404 for an unknown story or, on a real run, an unknown subject; 409
        ``revision_conflict`` for a stale expected revision (story scope); 503
        ``config_key_missing`` without ``EPISTREL_CONFIG_KEY``."""
        principal = self.require_principal(ctx)
        erasure.check_request(request)
        key = erasure.require_key(
            None
            if self.settings.config_key is None
            else self.settings.config_key.get_secret_value()
        )
        story_id = request.story_id
        if story_id is not None:
            # A request naming a story is gated like any call naming one (#383, #387): the
            # method has no ``story_id`` parameter, so ``gate_story_methods`` cannot see it.
            await self._erasure_gate(story_id)
            await check_expected_revision(self.db, story_id, ctx.expected_revision)
        elif await self._catalog_block() is not None:
            # A catalog-scope erasure, dry run or not, reads the whole catalog (its candidate
            # snippets quote it): refused while another is pending there (#346, as #383).
            async with self.db.connect() as conn:
                await erasure_ledger.refuse_catalog_pending(conn)
        compressor = self.ports.summary_compressor
        if request.dry_run:
            return await erasure.dry_run(
                self.db, request, compressor=compressor, principal=principal
            )

        async def collect(checksum: str) -> bool:
            return await media_sweep.collect_blob(self.db, self.media, checksum)

        async def run(_: RequestContext) -> erasure.ErasureResult:
            return await erasure.erase(
                self.db,
                request,
                principal=principal,
                config_key=key,
                compressor=compressor,
                collect=collect,
                ledger=self.ledger,
            )

        return await self._keyed(ctx, "erase", run, material={"body": _body(request)})

    # -- the erasure ledger (FR-ADMIN-7,8; #125) --------------------------------------------------

    @library_only("lifecycle: the REST app runs it at start-up, before it serves")
    async def start_erasure_reconcile(self) -> None:
        """Compare the erasure ledger's file with the database before traffic (``mark``:
        stories a restore unerased become ``blocked_by_erasure``), then reapply what is pending
        in the background within ``REAPPLY_BUDGET_S`` (entries still pending are retried by
        ``erasure_reconcile_retry``). A broken or tampered ledger raises (``LedgerCorrupt``,
        ``LedgerMismatch``): the process must not serve. An unreachable database does not stop
        the start: the first call naming a story compares again."""
        try:
            await self._mark_erasures(strict=True)
        except erasure_ledger.LedgerCorrupt, erasure_ledger.LedgerMismatch:
            raise
        except Exception as exc:  # the database is down: compared on the first story call
            logger.warning(
                "the erasure ledger was not compared at start-up: %s", type(exc).__name__
            )
            return
        self._reconcile_task = asyncio.create_task(
            self._reapply_within_budget(), name="epistrel-erasure-reconcile"
        )

    async def _reapply_within_budget(self) -> None:
        try:
            async with asyncio.timeout(erasure_ledger.REAPPLY_BUDGET_S):
                await self._reconcile_run(mark=False)
        except TimeoutError:
            logger.warning("the erasure reapply ran out of its start-up budget; retried later")
        except Exception as exc:
            logger.warning("the erasure reapply failed at start-up: %s", type(exc).__name__)

    async def _mark_erasures(self, *, strict: bool = False) -> erasure_ledger.MarkReport:
        """One ``mark`` pass. ``strict`` raises the ledger's own errors (start-up refuses to
        serve); otherwise they are 503 ``ledger_unavailable``."""
        async with self._mark_lock:
            try:
                report = await erasure_ledger.mark(self.db, self.ledger)
            except erasure_ledger.LedgerCorrupt as exc:
                if strict:
                    raise
                raise LedgerUnavailable(str(exc), {"reason": exc.reason}) from None
            except erasure_ledger.LedgerMismatch as exc:
                if strict:
                    raise
                raise LedgerUnavailable(str(exc), {"reason": "mismatch"}) from None
            except OSError as exc:
                raise LedgerUnavailable(
                    f"the erasure ledger file could not be read: {type(exc).__name__}",
                    {"reason": "unreadable"},
                ) from None
            self._erasure_marked = True
            return report

    async def _replay_gate(
        self, stored: idempotency.StoredResult, record: tuple[str, str] | None = None
    ) -> None:
        """A stored result naming a story (``create_story``'s, under a deployment-scoped key
        the story gate never sees) is answered only while the story is not blocked: 503
        ``erasure_pending`` otherwise, as every read of it is (#369). A deployment-scoped
        result (``record``, its index record) a pending catalog-scope erasure reaches is 503
        too (#346). The reapply purges the result, so a later replay answers the reference."""
        body = stored.body
        story_id = body.get("story_id") if isinstance(body, Mapping) else None
        if isinstance(story_id, str):
            await self._erasure_gate(story_id)
        if record is not None and (block := await self._catalog_block()) is not None:
            block.check_records([record])

    async def _erasure_gate(self, story_id: str) -> None:
        """Every call naming a story: 503 ``erasure_pending`` while it is blocked."""
        if not self._erasure_marked:
            await self._mark_erasures()
        await erasure_ledger.check_story(self.db, story_id)

    async def _catalog_block(self) -> catalog_block.CatalogBlock | None:
        """What the pending catalog-scope erasures reach (#346, ADR-157), the ledger compared
        first as for a story; ``None`` while none is pending."""
        if not self._erasure_marked:
            await self._mark_erasures()
        async with self.db.connect() as conn:
            return await catalog_block.read(conn)

    async def _policy_gate(self, policy_id: str) -> None:
        """A call naming a policy: 503 ``erasure_pending`` while a pending catalog-scope erasure
        reaches its text (#429)."""
        if (block := await self._catalog_block()) is not None:
            block.check_records([(catalog_block.POLICIES, policy_id)])

    async def _catalog_gate(self, kind: str, entry_id: str) -> None:
        """Every call naming a catalog entry: 503 ``erasure_pending`` while a pending
        catalog-scope erasure reaches it."""
        if (block := await self._catalog_block()) is not None:
            block.check_entry(kind, entry_id)

    async def _reapply_one(self, pending: erasure_ledger.Pending) -> str:
        """#124's erasure again, with the entry's own id, principal ``system`` and no new
        ledger line."""
        scope: dict[str, Any] = (
            {"story": pending.story_id} if pending.story_id is not None else {"catalog": True}
        )
        request = erasure.ErasureRequest.model_validate(
            {"subject": {"kind": pending.subject_kind, "id": pending.subject_id}, "scope": scope}
        )
        key = erasure.require_key(
            None
            if self.settings.config_key is None
            else self.settings.config_key.get_secret_value()
        )

        async def collect(checksum: str) -> bool:
            return await media_sweep.collect_blob(self.db, self.media, checksum)

        try:
            await erasure.erase(
                self.db,
                request,
                principal=SYSTEM_PRINCIPAL,
                config_key=key,
                compressor=self.ports.summary_compressor,
                collect=collect,
                erasure_id=pending.erasure_id,
                write_ledger=False,
                confirmed=pending.confirmed,
            )
        except SubjectNotFound:
            return erasure_ledger.SUBJECT_MISSING
        except StoryNotFound:
            return erasure_ledger.STORY_MISSING
        return erasure_ledger.REAPPLIED

    async def _reconcile_run(self, *, mark: bool = True) -> erasure_ledger.ReapplyReport:
        """``mark`` (unless start-up just ran it), then ``reapply``; one summary line."""
        marked = await self._mark_erasures() if mark else erasure_ledger.MarkReport()
        report = await erasure_ledger.reapply(self.db, self._reapply_one)
        erasure_ledger.log_summary(marked, report)
        return report

    @refuses_nul()
    async def erasure_status(self, ctx: RequestContext) -> erasure_ledger.ReconcileStatus:
        """The erasure ledger's reconciliation (FR-ADMIN-7): the entries this database has not
        applied yet (``pending``, each with its story) and their count, how many are applied,
        whether a reapply is running, and the last run's time, error (an exception class) and
        attempts per pending entry."""
        return await erasure_ledger.status(self.db)

    async def reconcile_erasure(self, ctx: RequestContext) -> erasure_ledger.ReconcileRun:
        """Run the erasure reconciler now (FR-ADMIN-7): compare the ledger's file with the
        database (blocking the stories a restore unerased), then reapply every pending entry.
        ``ran`` is false when another process was already running it. It records no operation
        and takes no ``Idempotency-Key``: running it again is harmless."""
        self.require_principal(ctx)
        report = await self._reconcile_run()
        async with self.db.connect() as conn:
            pending = await erasure_ledger.pending_count(conn)
        return erasure_ledger.ReconcileRun(
            run_id=new_ulid(),
            ran=report.ran,
            settled=len(report.settled),
            failed=len(report.failed),
            pending_count=pending,
        )

    @refuses_nul()
    async def erasure_boundary(self, ctx: RequestContext) -> erasure_boundary.ErasureBoundary:
        """What an erasure covers and what it cannot reach (FR-ADMIN-8), as data: the same
        text as ``docs/OPERATIONS.md`` and the API description."""
        return erasure_boundary.ERASURE_BOUNDARY

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

    @refuses_nul()
    async def get_media(self, ctx: RequestContext, media_id: str) -> media.MediaAsset:
        """An asset's metadata, ``live`` or ``retired``; 404 for an unknown asset."""
        return await media.get(self.db, media_id)

    @refuses_nul()
    async def get_media_content(
        self,
        ctx: RequestContext,
        media_id: str,
        *,
        exp: int | None = None,
        sig: str | None = None,
        byte_range: media.ByteRange | None = None,
        if_none_match: str | Sequence[str] = (),
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

    @refuses_nul()
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

    @refuses_nul()
    async def list_media(
        self,
        ctx: RequestContext,
        *,
        owner: str | None = None,
        relation: str | Sequence[str] = (),
        kind: MediaKind | None = None,
        state: MediaState = MediaState.LIVE,
        limit: int = media.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> media.MediaPage:
        """``live`` assets (``state=retired``: the deleted ones), newest first, a page at a time
        (``cursor``). ``owner``, ``relation`` and ``kind`` narrow the list only when given
        (FR-AUTH-8). An asset a pending catalog-scope erasure reaches is left out until it has
        been reapplied (#346)."""
        page = await media.list_assets(
            self.db,
            owner=ownership.owner_filter(owner),
            relations=[ownership.parse_relation_filter(value) for value in relation],
            kind=kind,
            state=state,
            limit=limit,
            cursor=cursor,
        )
        if (block := await self._catalog_block()) is None:
            return page
        return dataclasses.replace(
            page, items=[a for a in page.items if not block.hides(catalog_block.MEDIA, a.id)]
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

    @refuses_nul()
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
    # ``control`` operation, keyed per deployment. A new version is a configuration change, and
    # every story that follows the profile runs under it from its next turn (#113).

    @property
    def purposes(self) -> Purposes:
        """The model purposes a profile may route: the Orchestrator's delivered story purposes,
        the ones it reserves for later milestones (naming one is naming something undelivered),
        and the deployment-level ones no profile routes (``rating_suggestion``, #121;
        ``quality_judge``, #134)."""
        return Purposes.of(
            list(model_registry.STORY_PURPOSES),
            list(model_registry.RESERVED),
            [
                name
                for name in model_registry.DELIVERED
                if name not in model_registry.STORY_PURPOSES
            ],
        )

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

    @refuses_nul()
    async def get_profile(
        self, ctx: RequestContext, profile_id: str, *, version: int | None = None
    ) -> profiles.EngineProfile:
        """The profile at its latest version (``version``: that one), deleted or not; 404 for an
        unknown profile or version."""
        return await profiles.get_profile(self.db, profile_id, version=version)

    @refuses_nul()
    async def get_profile_guarantees(
        self, ctx: RequestContext, profile_id: str, *, version: int | None = None
    ) -> guarantees.GuaranteeTable:
        """The guarantee table (FR-VERIFY-15, FR-PROF-12; #132) of the profile at its latest
        version (``version``: that one) over the deployment's defaults, story overrides
        excluded: each step, deterministic or model-dependent with its metric, and whether the
        profile provides it. 404 for an unknown profile or version."""
        return await profiles.profile_guarantees(self.db, profile_id, version=version)

    @refuses_nul()
    async def list_profiles(
        self,
        ctx: RequestContext,
        *,
        owner: str | None = None,
        relation: str | Sequence[str] = (),
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
        undelivered settings. A valid result warns (``below_safety_minimum``, #156) when the
        profile's call cap is below a turn's safety minimum with the deployment's global
        model-assisted policies. 404 for an unknown profile."""
        return await profiles.validate_stored(
            self.db,
            profile_id,
            request,
            purposes=self.purposes,
            guardrail_calls=await policy_store.deployment_guardrail_calls(self.db),
        )

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

    @refuses_nul()
    async def list_profile_versions(
        self, ctx: RequestContext, profile_id: str
    ) -> list[profiles.ProfileVersion]:
        """Every version of the profile, oldest first: number, time and author."""
        return await profiles.list_versions(self.db, profile_id)

    @refuses_nul()
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
        ``request.overrides`` is a JSON merge patch over the story's overrides, applied as
        ``set_profile_overrides`` applies one, so the report is the one that write would answer.
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

    @refuses_nul()
    async def get_effective_config(
        self, ctx: RequestContext, story_id: str
    ) -> effective.EffectiveConfig:
        """The story's effective configuration (FR-PROF-5,16): the deployment's defaults, the
        profile and the version it runs on (``pinned`` when an unavailable upgrade kept an
        earlier one), its overrides with their revision, every delivered setting at its
        effective value (defaults, then the profile's version, then the overrides), the
        fingerprint, the capabilities and the quality status. 404 for an unknown story."""
        return await effective.read(self.db, story_id, await self._runtime_facts())

    @refuses_nul()
    async def get_story_guarantees(
        self, ctx: RequestContext, story_id: str
    ) -> guarantees.GuaranteeTable:
        """The guarantee table (FR-VERIFY-15, FR-PROF-12; #132) of the story's effective
        configuration, its overrides included, with the profile and version it runs on. 404 for
        an unknown story."""
        return await effective.read_guarantees(self.db, story_id)

    async def assign_engine_profile(
        self, ctx: RequestContext, story_id: str, request: story_profile.ProfileAssignment
    ) -> TwoStageResult[effective.EffectiveConfig]:
        """Run the story under another engine profile, at its latest version, from its next turn
        (FR-PROF-1,7). The story keeps its overrides, and the new combination must run (422
        ``invalid_profile``, naming them). Two-stage (#109): a change its impact report warns
        of applies only with ``request.ignore_warnings``; otherwise the result is the report,
        nothing is written and the ``Idempotency-Key`` is released. The story's own profile
        changes nothing. A story-scoped configuration change: the story's ``config_revision``
        moves. A confirmed assignment whose upgrade is unavailable (M3's interim rule) is
        applied with the story's configuration as it was, and writes nothing. 404 for an unknown
        story, 422 ``profile_deleted`` for a profile that cannot be assigned, 409
        ``story_archived`` for an archived story when the call would change something (naming
        the story's own profile answers 200 and writes nothing)."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> TwoStageResult[effective.EffectiveConfig]:
            await self.check_expected_revision(ctx, story_id)
            return await story_profile.assign_profile(
                self.db,
                story_id,
                request,
                principal=principal,
                facts=await self._runtime_facts(),
                purposes=self.purposes,
            )

        return await self._keyed(
            ctx,
            "assign_engine_profile",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
            keep=TwoStageResult.kept,
        )

    async def set_profile_overrides(
        self, ctx: RequestContext, story_id: str, request: story_profile.ProfileOverridesPatch
    ) -> TwoStageResult[effective.EffectiveConfig]:
        """Change the story's overrides of its profile's dials (FR-PROF-5) with a JSON merge
        patch (``null`` removes an override). The overrides must be a settings document of
        delivered settings and the whole configuration must run (422 ``validation_error`` or
        ``invalid_profile``); ``request.expected_revision``, when given, must be the overrides'
        revision (409 ``revision_conflict``). Two-stage (#109), like an assignment. A
        story-scoped configuration change: the story's ``config_revision`` and the overrides'
        revision move. A confirmed patch whose upgrade is unavailable (M3's interim rule) is
        applied with the story's configuration as it was, and writes nothing. 404 for an unknown
        story, 409 ``story_archived`` for an archived story when the call would change
        something (a patch that changes no setting answers 200 and writes nothing)."""
        principal = self.require_principal(ctx)

        async def run(ctx: RequestContext) -> TwoStageResult[effective.EffectiveConfig]:
            await self.check_expected_revision(ctx, story_id)
            return await story_profile.set_overrides(
                self.db,
                story_id,
                request,
                principal=principal,
                facts=await self._runtime_facts(),
                purposes=self.purposes,
            )

        return await self._keyed(
            ctx,
            "set_profile_overrides",
            run,
            story_id=story_id,
            material={"story_id": story_id, "body": _body(request)},
            keep=TwoStageResult.kept,
        )

    # -- message feedback (FR-FB-1,2; #112) --------------------------------------------------------
    # A rating is a plain table write, not an operation: no ``Idempotency-Key`` (a repeated
    # request converges on the same row; REST flags the routes ``mutating=False``), no expected
    # revision, and nothing a story view, prompt or retrieval reads.

    async def rate_message(
        self,
        ctx: RequestContext,
        story_id: str,
        message_id: str,
        request: feedback.FeedbackRequest,
    ) -> feedback.FeedbackWrite:
        """Rate a generated message as the requesting principal (FR-FB-1): ``message_id`` is its
        ``message_rendered`` event's id. The first rating is ``created`` (REST 201) with the
        context the message was produced under; a different value replaces it and joins the
        history (200); the same value again changes nothing (200). Allowed on an archived story
        and during a turn. 404 for an unknown story or a message that is not a generated message
        of it; 422 ``unknown_feedback_tag``; 422 ``validation_error`` at ``body.text`` over the
        deployment's ``feedback_text_max_chars``."""
        principal = self.require_principal(ctx)
        return await feedback.rate(self.db, story_id, message_id, request, principal=principal)

    async def retract_feedback(
        self, ctx: RequestContext, story_id: str, message_id: str
    ) -> feedback.FeedbackRetraction:
        """Retract the requesting principal's rating of the message (FR-FB-1): its final value
        stays in the history. Nothing to retract is not an error (REST 204 either way). 404 for
        an unknown story or message."""
        principal = self.require_principal(ctx)
        return await feedback.retract(self.db, story_id, message_id, principal=principal)

    @refuses_nul()
    async def get_feedback(
        self, ctx: RequestContext, story_id: str, message_id: str
    ) -> feedback.Feedback:
        """The requesting principal's own rating of the message, with its history, newest first
        (``rating`` null when it has none). It needs the principal, whose rating it is (400
        ``principal_required``), and no viewpoint: a rating is never story content. 404 for an
        unknown story or message."""
        value, problem = requesting_principal(ctx.principal)
        if problem is not None or value is None:
            raise PrincipalRequired(
                f"reading a rating needs the requesting principal: it is theirs ({problem})"
            )
        return await feedback.read(self.db, story_id, message_id, principal=value)

    # -- feedback reporting and export (FR-FB-3,4, FR-ADMIN-8; #126) -------------------------------
    # Flag-only: these read feedback for people to look at; nothing in a turn, a profile or a
    # guardrail reads them (the import-linter contract ``feedback-is-flag-only``).

    @refuses_nul()
    async def feedback_aggregates(
        self,
        ctx: RequestContext,
        group_by: feedback_reporting.FeedbackGroupBy,
        *,
        start: datetime | None = None,
        end: datetime | None = None,
        story_id: str | None = None,
    ) -> feedback_reporting.FeedbackAggregates:
        """The current ratings grouped by one dimension (FR-FB-3), each group with its counts,
        mean, histogram, tags and implicit signals (regenerations, regenerations soon after a
        low rating, swaps back, rewinds), and the totals. ``start``/``end`` bound when a rating
        was first given (inclusive, exclusive); ``story_id`` narrows to one story. 422 for a
        window that ends before it starts; 404 for an unknown story."""
        return await feedback_reporting.aggregates(
            self.db, group_by, start=start, end=end, story_id=story_id
        )

    @refuses_nul()
    async def feedback_review(
        self,
        ctx: RequestContext,
        review: feedback_reporting.FeedbackReviewList,
        *,
        story_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        tag: str | None = None,
        limit: int = feedback_reporting.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> feedback_reporting.FeedbackReviewPage:
        """A page of the ``guardrail`` or ``memory`` review list (FR-FB-3): the current ratings
        carrying the list's tags, newest first, with the message, the input it answered and its
        context. 422 for a window that ends before it starts, a tag not on the list or a cursor
        another listing gave; 404 for an unknown story. Ratings of a story the erasure
        reconciler holds blocked are left out; the ledger is compared first (#360)."""
        if not self._erasure_marked:
            await self._mark_erasures()
        return await feedback_reporting.review(
            self.db,
            review,
            story_id=story_id,
            start=start,
            end=end,
            tag=tag,
            limit=limit,
            cursor=cursor,
        )

    async def export_feedback(
        self, ctx: RequestContext, request: feedback_export.FeedbackExportRequest
    ) -> feedback_export.FeedbackExportWritten:
        """Export the current ratings as labelled cases (FR-FB-4): one JSON Lines file in the
        blob store, recorded in the export log with the principal. 403
        ``feedback_export_disabled`` while the deployment's ``feedback_export`` is off (checked
        after the request and before its ``Idempotency-Key``, so a refusal writes nothing); 422
        for a window that ends before it starts or ``min_rating`` above ``max_rating``. Keyed per
        deployment: a retry returns the stored answer and writes no second file. Ratings of a
        story the erasure reconciler holds blocked are left out; the ledger is compared first
        (#360)."""
        principal = self.require_principal(ctx)
        feedback_export.check_request(request)
        await feedback_export.require_enabled(self.db)
        if not self._erasure_marked:
            await self._mark_erasures()
        return await self._keyed(
            ctx,
            "export_feedback",
            lambda _: feedback_export.export(self.db, self.media, request, principal=principal),
            material={"body": request.model_dump(mode="json", by_alias=True)},
        )

    @refuses_nul()
    async def list_feedback_exports(
        self,
        ctx: RequestContext,
        *,
        limit: int = feedback_export.DEFAULT_PAGE,
        cursor: str | None = None,
    ) -> feedback_export.FeedbackExportPage:
        """The export log (FR-FB-4), newest first: who exported what, and when."""
        return await feedback_export.list_exports(self.db, limit=limit, cursor=cursor)

    @refuses_nul()
    async def get_feedback_export(
        self, ctx: RequestContext, export_id: str
    ) -> feedback_export.FeedbackExport:
        """One export's record; 404 for an unknown export."""
        return await feedback_export.read(self.db, export_id)

    @refuses_nul()
    async def get_feedback_export_content(
        self, ctx: RequestContext, export_id: str
    ) -> feedback_export.FeedbackExportContent:
        """The export's file (``application/x-ndjson``), delivered: the first download marks the
        export ``downloaded_at``, and from then on it is outside the erasure boundary
        (FR-ADMIN-8). 404 for an unknown export or one whose file was deleted. An undelivered
        export with a case of a story the erasure reconciler holds blocked (#370), or while a
        catalog-scope erasure is pending (#378), is 503 ``erasure_pending`` and stays
        undelivered; the ledger is compared first."""
        if not self._erasure_marked:
            await self._mark_erasures()
        return await feedback_export.content(self.db, self.media, export_id)

    async def delete_feedback_export(
        self, ctx: RequestContext, export_id: str
    ) -> feedback_export.FeedbackExportDeleted:
        """Delete the export's file; its record stays, marked with who deleted it and when.
        Deleting it again changes nothing (REST 204 either way); 404 for an unknown export. The
        delete is idempotent by itself and takes no ``Idempotency-Key``."""
        principal = self.require_principal(ctx)
        return await feedback_export.delete(self.db, self.media, export_id, principal=principal)


ERASURE_GATE_EXEMPT = frozenset({"delete_story"})
"""Methods naming a story that a blocked story still answers (#125): deleting it settles its
pending erasures. Every other public coroutine method naming a story refuses a story the
erasure reconciler blocked (503 ``erasure_pending``): one with a ``story_id`` parameter, and one
with ``entity_type`` and ``entity_id`` parameters when ``entity_type`` is ``story`` (the
ownership and relation methods behind ``/stories/{story_id}/owner`` and ``.../relations``)."""
ERASURE_GATED_ATTRIBUTE = "__epistrel_erasure_gated__"


def _erasure_gated[F: Callable[..., Awaitable[Any]]](method: F) -> F:
    signature = inspect.signature(method)

    @functools.wraps(method)
    async def gated(self: Engine, *args: Any, **kwargs: Any) -> Any:
        story_id = _named_story(signature.bind_partial(self, *args, **kwargs).arguments)
        if story_id is not None:
            await self._erasure_gate(story_id)
        return await method(self, *args, **kwargs)

    setattr(gated, ERASURE_GATED_ATTRIBUTE, True)
    return gated  # type: ignore[return-value]


def _names_a_story(parameters: Mapping[str, Any]) -> bool:
    """Whether a method with ``parameters`` can name a story (#125)."""
    return "story_id" in parameters or {"entity_type", "entity_id"} <= parameters.keys()


def _named_story(arguments: Mapping[str, Any]) -> str | None:
    """The story a call's bound ``arguments`` name: its ``story_id``, or its ``entity_id`` when
    its ``entity_type`` is ``story``. An unknown entity type names none here: the method itself
    answers it (422 ``validation_error``)."""
    story_id = arguments.get("story_id")
    if isinstance(story_id, str):
        return story_id
    entity_id = arguments.get("entity_id")
    if isinstance(entity_id, str) and arguments.get("entity_type") == EntityType.STORY.value:
        return entity_id
    return None


def gate_story_methods(engine_class: type) -> None:
    """Put the erasure block (#125) in front of every public coroutine method of
    ``engine_class`` that can name a story (a ``story_id``, or an ``entity_type`` and
    ``entity_id``), except ``ERASURE_GATE_EXEMPT``: one check in the Engine, so the REST API and
    the in-process library refuse alike."""
    for name, member in list(vars(engine_class).items()):
        if name.startswith("_") or name in ERASURE_GATE_EXEMPT:
            continue
        if not inspect.iscoroutinefunction(member) or getattr(
            member, ERASURE_GATED_ATTRIBUTE, False
        ):
            continue
        if _names_a_story(inspect.signature(member).parameters):
            setattr(engine_class, name, _erasure_gated(member))


CATALOG_PARAMETERS: Mapping[str, str] = {
    "character_id": catalog_block.CHARACTER,
    "scenario_id": catalog_block.SCENARIO,
    "persona_id": catalog_block.PERSONA,
    "media_id": catalog_block.MEDIA,
    "upload_id": catalog_block.UPLOAD,
}
"""The parameters that name a catalog entry (in a method without a ``story_id``), and its kind
(#346). ``entity_type`` with ``entity_id`` or ``entry_id`` names one when the type is a catalog
kind (``CATALOG_ENTITY_TYPES``)."""
CATALOG_ENTITY_TYPES = frozenset(
    {catalog_block.CHARACTER, catalog_block.SCENARIO, catalog_block.PERSONA, catalog_block.MEDIA}
)
CATALOG_GATED_ATTRIBUTE = "__epistrel_catalog_gated__"


def _names_an_entry(parameters: Mapping[str, Any]) -> bool:
    """Whether a method with ``parameters`` can name a catalog entry (#346). A method naming a
    story is the story's: its ``character_id`` is a story character."""
    if "story_id" in parameters:
        return False
    return any(name in parameters for name in CATALOG_PARAMETERS) or (
        "entity_type" in parameters and bool({"entity_id", "entry_id"} & parameters.keys())
    )


def _named_entry(arguments: Mapping[str, Any]) -> tuple[str, str] | None:
    """The catalog entry a call's bound ``arguments`` name, as ``(kind, id)``."""
    for name, kind in CATALOG_PARAMETERS.items():
        value = arguments.get(name)
        if isinstance(value, str):
            return kind, value
    entity_type = arguments.get("entity_type")
    entity_id = arguments.get("entity_id", arguments.get("entry_id"))
    if isinstance(entity_id, str) and isinstance(entity_type, str):
        kind = str(entity_type)
        if kind in CATALOG_ENTITY_TYPES:
            return kind, entity_id
    return None


def _catalog_entries_of(request: StoryFromScenario) -> list[tuple[str, str]]:
    """The catalog entries a story created from a scenario copies: the scenario, the catalog
    characters filling its roles, and the players' saved personas."""
    found = [(catalog_block.SCENARIO, str(request.scenario.id))]
    found += [
        (catalog_block.CHARACTER, str(slot.catalog_id))
        for slot in request.slots.values()
        if slot.catalog_id is not None
    ]
    for player in request.players.values():
        persona_id = getattr(player.persona, "id", None)
        if isinstance(persona_id, str):
            found.append((catalog_block.PERSONA, persona_id))
    return found


def _catalog_gated[F: Callable[..., Awaitable[Any]]](method: F) -> F:
    signature = inspect.signature(method)

    @functools.wraps(method)
    async def gated(self: Engine, *args: Any, **kwargs: Any) -> Any:
        entry = _named_entry(signature.bind_partial(self, *args, **kwargs).arguments)
        if entry is not None:
            await self._catalog_gate(*entry)
        return await method(self, *args, **kwargs)

    setattr(gated, CATALOG_GATED_ATTRIBUTE, True)
    return gated  # type: ignore[return-value]


def gate_catalog_methods(engine_class: type) -> None:
    """Put the catalog's restore-window block (#346, ADR-157) in front of every public coroutine
    method of ``engine_class`` that can name a catalog entry (``_names_an_entry``), reads and
    writes alike, deletes included (deleting the subject would take the terms its reapply
    redacts by): one check in the Engine, so REST and the library refuse alike."""
    for name, member in list(vars(engine_class).items()):
        if name.startswith("_") or not inspect.iscoroutinefunction(member):
            continue
        if getattr(member, CATALOG_GATED_ATTRIBUTE, False):
            continue
        if _names_an_entry(inspect.signature(member).parameters):
            setattr(engine_class, name, _catalog_gated(member))


NUL_GATED_PARAMETERS = frozenset({"story_id", "entity_id", "entry_id", *CATALOG_PARAMETERS})
"""What the erasure and catalog gates (#125, #346) look up before a method runs: in a method
that is not a ``refuses_nul`` read (a mutation), these are the arguments checked for NUL ahead
of the gates, which would otherwise pass one to SQL before ``_keyed`` refuses it (#278)."""


def _plain(value: Any) -> Any:
    """``value`` as the JSON-like data ``refuse_nul`` walks: strings, and sequences and mappings
    of them (``None`` for anything else: a model, bytes, a number)."""
    if isinstance(value, str | dict | list | tuple):
        return value
    if isinstance(value, Mapping):
        return dict(value)
    if isinstance(value, Sequence | set | frozenset) and not isinstance(value, bytes | bytearray):
        return list(value)
    return None


def nul_location_of(parameter: inspect.Parameter) -> tuple[str, ...]:
    """The field a NUL in ``parameter`` is refused at: its name when positional (as ``_keyed``
    names a ``story_id``), ``query.<name>`` when keyword-only (as the reads with their own
    filters name theirs)."""
    keyword = parameter.kind is inspect.Parameter.KEYWORD_ONLY
    return ("query", parameter.name) if keyword else (parameter.name,)


def nul_guarded_parameters(method: Callable[..., Any]) -> list[inspect.Parameter]:
    """The parameters of an ``Engine`` coroutine method that ``guard_nul_methods`` checks: every
    one of a ``refuses_nul`` read but ``self``, ``ctx`` and its ``own``; a gated method's
    ``NUL_GATED_PARAMETERS``; none otherwise."""
    parameters = inspect.signature(method).parameters
    own = getattr(method, NUL_CHECKED_ATTRIBUTE, None)
    if isinstance(own, tuple):
        return [p for name, p in parameters.items() if name not in {"self", "ctx", *own}]
    if getattr(method, ERASURE_GATED_ATTRIBUTE, False) or getattr(
        method, CATALOG_GATED_ATTRIBUTE, False
    ):
        return [p for name, p in parameters.items() if name in NUL_GATED_PARAMETERS]
    return []


def _nul_guarded[F: Callable[..., Awaitable[Any]]](
    method: F, checked: Sequence[inspect.Parameter]
) -> F:
    signature = inspect.signature(method)
    locations = [(parameter.name, nul_location_of(parameter)) for parameter in checked]

    @functools.wraps(method)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError:
            return await method(*args, **kwargs)  # the call's own TypeError
        for name, loc in locations:
            if name in bound.arguments:
                refuse_nul(_plain(bound.arguments[name]), loc)
        return await method(*args, **kwargs)

    return guarded  # type: ignore[return-value]


def guard_nul_methods(engine_class: type) -> None:
    """Refuse a NUL character (422 ``validation_error``, ``string_nul``: ``refuse_nul``, as #73
    made it for request bodies) in the arguments of every ``refuses_nul`` read and in the
    arguments the gates look up, before the gates run (#278). Run after the gates are in place,
    so the check is in front of them."""
    for name, member in list(vars(engine_class).items()):
        if name.startswith("_") or not inspect.iscoroutinefunction(member):
            continue
        checked = nul_guarded_parameters(member)
        if checked:
            setattr(engine_class, name, _nul_guarded(member, checked))


REPEATED_ATTRIBUTE = "__epistrel_repeated__"

_STRING_COLLECTIONS: tuple[Any, ...] = (
    Sequence,
    collections.abc.Iterable,
    collections.abc.Collection,
    collections.abc.Set,
    list,
    tuple,
    set,
    frozenset,
)


def _is_strings(annotation: Any) -> bool:
    """Whether ``annotation`` (or a member of its union) is a collection of ``str``:
    ``Sequence[str]``, ``Iterable[str]``, ``list[str]``, ``tuple[str, ...]`` and the like."""
    if isinstance(annotation, types.UnionType) or typing.get_origin(annotation) is typing.Union:
        return any(_is_strings(member) for member in typing.get_args(annotation))
    arguments = typing.get_args(annotation)
    return (
        typing.get_origin(annotation) in _STRING_COLLECTIONS
        and bool(arguments)
        and all(argument is str or argument is Ellipsis for argument in arguments)
    )


def repeated_parameters(method: Callable[..., Any]) -> tuple[str, ...]:
    """The parameters of ``method`` typed as a collection of strings (a repeatable filter, or
    a list of ids): the ones ``normalise_repeated_methods`` takes a plain string in as one
    value."""
    hints = typing.get_type_hints(inspect.unwrap(method))
    return tuple(
        name
        for name in inspect.signature(method).parameters
        if name in hints and _is_strings(hints[name])
    )


def _repeated_normalised[F: Callable[..., Awaitable[Any]]](method: F, names: Sequence[str]) -> F:
    signature = inspect.signature(method)

    @functools.wraps(method)
    async def normalised(*args: Any, **kwargs: Any) -> Any:
        try:
            bound = signature.bind(*args, **kwargs)
        except TypeError:
            return await method(*args, **kwargs)  # the call's own TypeError
        for name in names:
            if isinstance(bound.arguments.get(name), str):
                bound.arguments[name] = (bound.arguments[name],)
        return await method(*bound.args, **bound.kwargs)

    setattr(normalised, REPEATED_ATTRIBUTE, tuple(names))
    return normalised  # type: ignore[return-value]


def normalise_repeated_methods(engine_class: type) -> None:
    """Take a plain string as one value in every collection-of-strings parameter of every
    public coroutine method of ``engine_class`` (#324, as #110 made it for the catalog search):
    a ``str`` is itself a ``Sequence[str]``, so ``relation="editor:p-c"`` was read one character
    at a time. One query parameter is one value over REST; the library now agrees. Run first,
    so the method's body (and nothing in front of it) sees the value as given."""
    for name, member in list(vars(engine_class).items()):
        if name.startswith("_") or not inspect.iscoroutinefunction(member):
            continue
        if names := repeated_parameters(member):
            setattr(engine_class, name, _repeated_normalised(member, names))


normalise_repeated_methods(Engine)
gate_story_methods(Engine)
gate_catalog_methods(Engine)
guard_nul_methods(Engine)
# One span, summary line and count per outermost call of every public coroutine method (#74).
instrument.instrument_engine(Engine)
