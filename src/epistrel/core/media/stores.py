"""Which blob store a deployment uses, and the start-up check that it did not change (#107).

``make_store`` builds the adapter ``EPISTREL_MEDIA_STORE`` names: ``local`` (the default, under
``EPISTREL_MEDIA_ROOT``) or ``s3`` (``core.media.s3``, whose ``boto3`` is imported only then).

``store_kind_problem`` is the start-up check (``epistrel serve``'s preflight and the app's
lifespan; exit 78): the kind is recorded in ``deployment_state.media_store`` at the first
start-up, and a later start with another kind is refused while any media asset exists, since the
assets' bytes are in the other store. With no asset yet, the new kind is recorded instead. An
unreachable database is not this check's problem (``/health`` reports it), so the check passes
unchecked, as the config-key check does.
"""

from __future__ import annotations

import logging

from sqlalchemy import exists, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncEngine

from epistrel.config import Settings
from epistrel.core.media.store import BlobStore, LocalFilesystemStore
from epistrel.core.tables import deployment_state, media_assets
from epistrel.core.tables.deployment import DEPLOYMENT_ROW_ID

logger = logging.getLogger(__name__)


def make_store(settings: Settings) -> BlobStore:
    """The blob store ``settings`` configure."""
    if settings.media_store == "s3":
        from epistrel.core.media.s3 import S3BlobStore

        assert settings.media_s3_bucket is not None  # noqa: S101 - Settings requires it for s3
        secret = settings.media_s3_secret_key
        return S3BlobStore(
            settings.media_root,
            bucket=settings.media_s3_bucket,
            endpoint=settings.media_s3_endpoint,
            public_endpoint=settings.media_s3_public_endpoint,
            region=settings.media_s3_region,
            access_key=settings.media_s3_access_key,
            secret_key=secret.get_secret_value() if secret is not None else None,
            path_style=settings.media_s3_path_style,
            verify_tls=settings.media_s3_verify_tls,
        )
    return LocalFilesystemStore(settings.media_root)


async def store_kind_problem(settings: Settings, db: AsyncEngine) -> str | None:
    """``None`` when ``settings.media_store`` may serve this deployment's media (recording it
    when nothing was recorded or no asset exists yet) or the database cannot be reached; the
    refusal's message otherwise."""
    try:
        return await _store_kind_problem(settings, db)
    except (DBAPIError, OSError) as exc:
        logger.warning("the media store kind was not checked at start-up: %s", type(exc).__name__)
        return None


async def _store_kind_problem(settings: Settings, db: AsyncEngine) -> str | None:
    kind = settings.media_store
    async with db.begin() as conn:
        recorded = await conn.scalar(
            select(deployment_state.c.media_store)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .with_for_update()
        )
        if recorded == kind:
            return None
        if recorded is not None and await conn.scalar(select(exists().select_from(media_assets))):
            return (
                f"EPISTREL_MEDIA_STORE is {kind}, but this deployment's media are in the "
                f"{recorded} store; switching stores while media exist is refused (copy the "
                f"blobs and change deployment_state.media_store, or set EPISTREL_MEDIA_STORE="
                f"{recorded})"
            )
        await conn.execute(
            update(deployment_state)
            .where(deployment_state.c.id == DEPLOYMENT_ROW_ID)
            .values(media_store=kind)
        )
    return None


__all__ = ["make_store", "store_kind_problem"]
