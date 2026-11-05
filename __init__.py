"""Retouch: mark the parts of an image that need work, discuss each mark with Hermes, and let it fix
only those areas. Agent half: four tools and a skill. The pane lives in ``desktop/plugin.js`` and talks
to ``dashboard/plugin_api.py``."""

from pathlib import Path

from . import tools


def register(ctx):
    tools.register(ctx)
    ctx.register_skill("retouch", Path(__file__).parent / "skills" / "retouch" / "SKILL.md")
