"""API routers: one per area, the agent contract and the PWA files."""

from .agent import router as agent_router
from .creative import router as creative_router
from .html_motion import router as html_motion_router
from .family import router as family_router
from .health import router as health_router
from .media import router as media_router
from .misc import router as misc_router
from .projects import router as projects_router
from .pwa import router as pwa_router

ROUTERS = [health_router, media_router, projects_router, misc_router, agent_router, family_router, creative_router, html_motion_router, pwa_router]
