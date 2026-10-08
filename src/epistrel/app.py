"""Epistrel Engine HTTP service entry point."""

from fastapi import FastAPI

app = FastAPI(title="Epistrel Engine")


@app.get("/health")
async def health() -> dict[str, str]:
    """Liveness probe."""
    return {"status": "ok"}
