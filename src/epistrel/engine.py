"""The in-process library: ``epistrel.Engine`` (docs/02 §9, surface 2).

Every REST handler is a thin wrapper that builds a ``RequestContext`` and calls one ``Engine``
method; the library needs no service credential (FR-AUTH-1). Operations arrive with later
stories; for now the Engine holds its settings and enforces the attribution rule.
"""

from __future__ import annotations

from epistrel.config import Settings
from epistrel.core.context import RequestContext
from epistrel.core.errors import PrincipalRequired


class Engine:
    """The async entry point to Epistrel."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    @classmethod
    def from_settings(cls, settings: Settings) -> Engine:
        return cls(settings)

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
