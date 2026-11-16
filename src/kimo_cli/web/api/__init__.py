"""API routes."""

from kimo_cli.web.api import admin as admin_module
from kimo_cli.web.api import admin_evolution as admin_evolution_module
from kimo_cli.web.api import admin_wiki as admin_wiki_module
from kimo_cli.web.api import auth_user, capabilities, config, memory, open_in, sessions
from kimo_cli.web.api import branding as branding_module
from kimo_cli.web.api import mcp as mcp_module

config_router = config.router
sessions_router = sessions.router
work_dirs_router = sessions.work_dirs_router
agents_router = sessions.agents_router
open_in_router = open_in.router
auth_router = auth_user.router
admin_router = admin_module.router
admin_evolution_router = admin_evolution_module.router
admin_wiki_router = admin_wiki_module.router
mcp_router = mcp_module.router
branding_public_router = branding_module.public_router
branding_admin_router = branding_module.admin_router
memory_router = memory.router
capabilities_router = capabilities.capabilities_router

__all__ = [
    "admin_evolution_router",
    "admin_router",
    "admin_wiki_router",
    "agents_router",
    "auth_router",
    "branding_admin_router",
    "branding_public_router",
    "capabilities_router",
    "config_router",
    "mcp_router",
    "memory_router",
    "open_in_router",
    "sessions_router",
    "work_dirs_router",
]
