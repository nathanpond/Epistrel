"""Image job errors raised below the Orchestrator (FR-IMG-11; #267)."""

from __future__ import annotations


class FencedToken(Exception):
    """The caller's attempt token is no longer the job's current one: a lease expiry, a retry
    or a cancel replaced it (``core.images.store.require_token``). A fenced worker may only
    settle its own attempt (``orchestrator.images.attempts.settle_attempt``); promotion,
    attachment and every other counter change refuse it. The message carries ids only."""

    def __init__(self, job_id: str, token: str) -> None:
        super().__init__(f"image job {job_id}: attempt {token} is fenced")
        self.job_id = job_id
        self.token = token


__all__ = ["FencedToken"]
