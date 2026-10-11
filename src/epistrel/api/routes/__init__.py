"""Routers aggregated for `create_app`.

``public_router`` holds the routes invariant 4 exempts from the service credential (``/health``
now, ``/metrics`` with #74). Everything else goes on ``router``, which ``create_app`` includes with
the credential and principal dependencies.
"""

from fastapi import APIRouter

from epistrel.api.routes import health

public_router = APIRouter()
public_router.include_router(health.router)

router = APIRouter()

__all__ = ["public_router", "router"]
