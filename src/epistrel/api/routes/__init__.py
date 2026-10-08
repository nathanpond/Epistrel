"""Routers aggregated into one for `create_app`."""

from fastapi import APIRouter

from epistrel.api.routes import health

router = APIRouter()
router.include_router(health.router)

__all__ = ["router"]
