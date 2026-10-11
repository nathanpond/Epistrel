"""Routers aggregated for `create_app`.

``public_router`` holds the routes invariant 4 exempts from the service credential: ``/health``
and ``/metrics`` (#74), which a ``worker``-mode process serves too (#71). Everything else goes on
``router``, which ``create_app`` includes with the credential and principal dependencies; that
includes the ``/admin`` operator router.
"""

from fastapi import APIRouter

from epistrel.api.routes import (
    admin,
    health,
    history,
    operations,
    respond,
    stories,
    story_texts,
    timeline,
)
from epistrel.observability import metrics

public_router = APIRouter()
public_router.include_router(health.router)
public_router.include_router(metrics.router)

router = APIRouter()
router.include_router(stories.router)
router.include_router(history.router)
router.include_router(respond.router)
router.include_router(story_texts.router)
router.include_router(timeline.router)
router.include_router(operations.router)
router.include_router(admin.router)

__all__ = ["public_router", "router"]
