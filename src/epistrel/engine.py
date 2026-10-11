"""The in-process library: ``epistrel.Engine`` (docs/02 §9, surface 2).

Every REST handler is a thin wrapper that builds a ``RequestContext`` and calls one ``Engine``
method; the library needs no service credential (FR-AUTH-1). The Engine holds its settings and
the database it was bound to (the REST app binds it for the life of the server).
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core import projections
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
        return await projections.rebuild(self.db, story_id, principal=principal)

    async def history(self, ctx: RequestContext, story_id: str) -> History:
        """The story's active transcript, read from the projection (#59 adds viewpoints)."""
        return await read_history(self.db, story_id)
