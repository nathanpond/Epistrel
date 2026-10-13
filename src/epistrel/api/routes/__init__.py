"""Routers aggregated for `create_app`.

``public_router`` holds the routes invariant 4 exempts from the service credential: ``/health``
and ``/metrics`` (#74), which a ``worker``-mode process serves too (#71). ``signed_router`` holds
the one route that takes the credential or a valid signed URL, ``GET /media/{id}/content``
(#107), which ``create_app`` includes with that guard in ``api`` and ``all`` modes only.
Everything else goes on ``router``, which ``create_app`` includes with the credential and
principal dependencies; that includes the ``/admin`` operator router.
"""

from fastapi import APIRouter

from epistrel.api.routes import (
    admin,
    catalog_characters,
    catalog_scenarios,
    engine_profiles,
    feedback,
    health,
    history,
    media,
    operations,
    respond,
    stories,
    story_profile,
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
router.include_router(catalog_characters.router)
router.include_router(catalog_scenarios.router)
router.include_router(media.router)
router.include_router(engine_profiles.router)
router.include_router(story_profile.router)
router.include_router(feedback.router)
router.include_router(admin.router)

signed_router = APIRouter()
signed_router.include_router(media.content_router)

__all__ = ["public_router", "router", "signed_router"]
