"""The in-process library: ``epistrel.Engine`` (docs/02 §9, surface 2).

Every REST handler is a thin wrapper that builds a ``RequestContext`` and calls one ``Engine``
method; the library needs no service credential (FR-AUTH-1). The Engine holds its settings and
the database it was bound to (the REST app binds it for the life of the server).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import deployment, projections, ratings
from epistrel.core.concurrency import check_expected_revision
from epistrel.core.context import RequestContext
from epistrel.core.errors import PrincipalRequired
from epistrel.core.history import History, read_history


class Engine:
    """The async entry point to Epistrel."""

    def __init__(self, settings: Settings, db: AsyncEngine | None = None) -> None:
        self.settings = settings
        self._db = db

    @classmethod
    def from_settings(cls, settings: Settings) -> Engine:
        return cls(settings)

    def bind(self, db: AsyncEngine | None) -> None:
        """Use ``db`` for every operation (``None`` unbinds)."""
        self._db = db

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
