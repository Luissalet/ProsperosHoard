"""/api/health and /api/status."""

from __future__ import annotations

from fastapi import APIRouter, Request

from .. import SERVICE, __version__
from ..hoard_link import family
from .deps import services

router = APIRouter(prefix="/api")


@router.get("/health")
def health(request: Request):
    # Cheap on purpose: the launcher, the hub and the MCP bridge poll this.
    svc = services(request)
    return {"service": SERVICE, "version": __version__, "dataDirConfigured": request.app.state.config.data_dir_configured,
            "counts": svc.counts(), "hoard_link": family.health_block()}


@router.get("/status")
def status(request: Request):
    return services(request).status()
