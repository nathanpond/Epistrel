"""The in-process library: ``epistrel.Engine`` (docs/02 §9, surface 2).

Every REST handler is a thin wrapper that builds a ``RequestContext`` and calls one ``Engine``
method; the library needs no service credential (FR-AUTH-1). The Engine holds its settings and
the database it was bound to (the REST app binds it for the life of the server).
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import SecretStr
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import deployment, ingest, projections, ratings, stories
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.context import RequestContext
from epistrel.core.enums import StoryStatus
from epistrel.core.errors import PrincipalRequired
from epistrel.core.history import History, read_history
from epistrel.core.policies.model import Policy, Scope
from epistrel.core.settings_schema import StorySettings
from epistrel.orchestrator.budget import DEFAULT_TURN_DEADLINE_S, TurnBudget
from epistrel.orchestrator.lite import pipeline as lite
from epistrel.orchestrator.lite.schemas import RespondRequest, RespondResult
from epistrel.orchestrator.models import ModelAccessLayer
from epistrel.orchestrator.models import config as model_config
from epistrel.orchestrator.policies import admin as policy_admin
from epistrel.orchestrator.policies import input_gate
from epistrel.orchestrator.policies import store as policy_store
from epistrel.orchestrator.policies.age import CastMember
from epistrel.orchestrator.policies.input_gate import EntryPoint, GateText, InputPass
from epistrel.orchestrator.policies.schemas import (
    PolicyFields,
    PolicyReplacement,
    StoryPolicyItem,
)


class Engine:
    """The async entry point to Epistrel."""

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

    @classmethod
    def from_settings(cls, settings: Settings) -> Engine:
        return cls(settings)

    def bind(self, db: AsyncEngine | None) -> None:
        """Use ``db`` for every operation (``None`` unbinds)."""
        self._db = db
        self.models.bind(db)

    @property
    def db(self) -> AsyncEngine:
        if self._db is None:
            raise RuntimeError("the Engine has no database; bind one first")
        return self._db

    @staticmethod
    def require_principal(ctx: RequestContext) -> str:
        """Every mutation carries a requesting principal (FR-AUTH-4); the library checks too."""
        if ctx.principal is None or not ctx.principal.strip():
            raise PrincipalRequired("a mutating call needs a requesting principal")
        return ctx.principal

    async def noop(self, ctx: RequestContext) -> str:
        """A mutating call that changes nothing and returns the recorded principal.

        Stands in for real operations (#45 onward) so the trust boundary can be proven before
        any exists; it is reachable only from the library and test-registered routes.
        """
        return self.require_principal(ctx)

    async def rebuild_projections(
        self, ctx: RequestContext, story_id: str
    ) -> projections.RebuildResult:
        """Refold every projection of ``story_id`` from the event log (FR-ADMIN-3).

        Synchronous in M2 whatever the story's size; #70 moves stories over
        ``settings.rebuild_async_threshold`` events to a background job.
        """
        principal = self.require_principal(ctx)
        await self.check_expected_revision(ctx, story_id)
        return await projections.rebuild(self.db, story_id, principal=principal)

    async def check_expected_revision(self, ctx: RequestContext, story_id: str) -> int:
        """Every story-scoped mutation starts here: a stale ``ctx.expected_revision`` is 409
        ``revision_conflict`` with ``current_revision`` before anything is written (FR-RESP-6).

        A start-time precondition only; the commit itself revalidates under FR-CONC-4.
        Deployment-scoped calls never call it, so the header is ignored there.
        """
        return await check_expected_revision(self.db, story_id, ctx.expected_revision)

    async def history(self, ctx: RequestContext, story_id: str) -> History:
        """The story's active transcript, read from the projection (#59 adds viewpoints)."""
        return await read_history(self.db, story_id)

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
    ) -> InputPass:
        """The input gate of a truth-changing path that accepts text (FR-GUARD-9,10): run it
        after the call's other prechecks (#44's order) and before its ``Operation`` opens. A
        violation writes the ``audit_log`` row and raises 422 ``guardrail_violation``; nothing
        else is written. Respond (#57), ingest (#58) and pins/instructions (#60) call it."""
        principal = self.require_principal(ctx)
        context = await policy_store.guard_context(self.db, story_id)
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
        )

    async def respond(
        self, ctx: RequestContext, story_id: str, request: RespondRequest
    ) -> RespondResult:
        """Play one Lite turn: the actor's input in, one finished reply out (FR-RESP-1).

        The consumer does no retrieval or memory work (FR-RESP-2): the prompt is assembled from
        the story's own records, never from ``scene_context`` (FR-RESP-4). The input is the
        turn's first beat (FR-RESP-9) and goes through the input gate before anything is
        recorded; the turn makes one ``generation`` call (FR-LITE-7). The message is
        *finished*: under ``lite`` none of its claims is checked."""
        principal = self.require_principal(ctx)

        async def gate(budget: TurnBudget, text: str, actor: str) -> InputPass:
            return await self.gate_input(
                ctx,
                story_id,
                EntryPoint.RESPOND,
                [GateText(lite.INPUT_LOCATOR, text)],
                actor=actor,
                budget=budget,
                reserved=1,
            )

        return await lite.respond(
            self.db, self.models, ctx, story_id, request, principal=principal, gate=gate
        )

    async def ingest(
        self, ctx: RequestContext, story_id: str, request: ingest.IngestRequest
    ) -> ingest.IngestResult:
        """Record consumer events into authoritative history without generating (FR-RESP-10).

        Each event is its own ``turn`` at the next index (revision +1), all or nothing; a
        ``client_event_id`` the story has seen returns the original result (same payload) or
        409 ``client_event_conflict`` (different payload), writing nothing. Every new event's
        text passes the input gate on its own before anything is recorded."""
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

        return await ingest.ingest(
            self.db,
            story_id,
            request,
            principal=principal,
            gate=gate,
            idempotency_key=ctx.idempotency_key,
            expected_revision=ctx.expected_revision,
            deadline_s=DEFAULT_TURN_DEADLINE_S,
        )

    async def story_settings(
        self, ctx: RequestContext, story_id: str
    ) -> tuple[StorySettings, policy_store.StoryGuardrails]:
        """The story's settings (defaults filled in) and its guardrails: effective policies
        with their kind and source, inactive ones, and the model calls they add (FR-GUARD-9)."""
        story = await stories.get_story(self.db, story_id)
        guardrails = await policy_store.story_guardrails(self.db, story_id)
        return StorySettings.from_row(story.settings), guardrails

    async def get_story(self, ctx: RequestContext, story_id: str) -> stories.StoryView:
        """The story and its cast; true ages only when ``ctx.viewpoint`` is ``omniscient``."""
        omniscient = ctx.viewpoint == stories.OMNISCIENT
        return await stories.get_story(self.db, story_id, omniscient=omniscient)

    async def list_stories(
        self, ctx: RequestContext, *, status: StoryStatus | None = None
    ) -> list[stories.StoryView]:
        return await stories.list_stories(self.db, status=status)

    async def archive_story(self, ctx: RequestContext, story_id: str) -> stories.StatusChange:
        """Make the story read-only; idempotent; 423 while a turn is in flight."""
        principal = self.require_principal(ctx)
        await self.check_expected_revision(ctx, story_id)
        return await stories.archive_story(self.db, story_id, principal=principal)

    async def unarchive_story(self, ctx: RequestContext, story_id: str) -> stories.StatusChange:
        principal = self.require_principal(ctx)
        await self.check_expected_revision(ctx, story_id)
        return await stories.unarchive_story(self.db, story_id, principal=principal)

    async def delete_story(self, ctx: RequestContext, story_id: str) -> dict[str, int]:
        """Remove every row of the story and nothing else; returns rows removed per table."""
        principal = self.require_principal(ctx)
        await self.check_expected_revision(ctx, story_id)
        return await stories.delete_story(self.db, story_id, principal=principal)

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
        return await ratings.create_rating(self.db, fields, principal=principal)

    async def update_content_rating(
        self, ctx: RequestContext, rating_id: str, fields: ratings.RatingReplacement
    ) -> ratings.RatingWrite:
        principal = self.require_principal(ctx)
        return await ratings.update_rating(self.db, rating_id, fields, principal=principal)

    async def retire_content_rating(
        self, ctx: RequestContext, rating_id: str
    ) -> ratings.RatingWrite:
        principal = self.require_principal(ctx)
        return await ratings.retire_rating(self.db, rating_id, principal=principal)

    async def deployment_settings(self, ctx: RequestContext) -> deployment.DeploymentSettings:
        return await deployment.read_settings(self.db)

    async def update_deployment_settings(
        self, ctx: RequestContext, patch: deployment.SettingsPatch
    ) -> deployment.DeploymentSettings:
        principal = self.require_principal(ctx)
        return await deployment.update_settings(self.db, patch, principal=principal)

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
        return await policy_admin.create_policy(
            self.db, fields, principal=principal, idempotency_key=ctx.idempotency_key
        )

    async def policies_update(
        self, ctx: RequestContext, policy_id: str, fields: PolicyReplacement
    ) -> policy_admin.PolicyWrite:
        principal = self.require_principal(ctx)
        return await policy_admin.update_policy(self.db, policy_id, fields, principal=principal)

    async def policies_retire(
        self, ctx: RequestContext, policy_id: str
    ) -> policy_admin.PolicyWrite:
        principal = self.require_principal(ctx)
        return await policy_admin.retire_policy(self.db, policy_id, principal=principal)

    async def policies_set_story(
        self, ctx: RequestContext, story_id: str, items: list[StoryPolicyItem]
    ) -> policy_admin.StoryPoliciesWrite:
        """Upsert the story's own policies (#63's ``PATCH /stories/{id}/settings``)."""
        principal = self.require_principal(ctx)
        await self.check_expected_revision(ctx, story_id)
        return await policy_admin.set_story_policies(self.db, story_id, items, principal=principal)

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
        return await model_config.replace_config(
            self.db, purpose, body, keybox=self.models.keybox, principal=principal
        )

    async def remove_model_override(
        self, ctx: RequestContext, purpose: str
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        return await model_config.remove_override(
            self.db, purpose, keybox=self.models.keybox, principal=principal
        )

    async def set_model_api_key(
        self, ctx: RequestContext, purpose: str, api_key: SecretStr
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        return await model_config.set_api_key(
            self.db, purpose, api_key, keybox=self.models.keybox, principal=principal
        )

    async def remove_model_api_key(
        self, ctx: RequestContext, purpose: str
    ) -> model_config.ConfigWrite:
        principal = self.require_principal(ctx)
        return await model_config.remove_api_key(
            self.db, purpose, keybox=self.models.keybox, principal=principal
        )
