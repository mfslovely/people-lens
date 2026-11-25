"""Importing this package registers every tool."""

from app.tools import actions, analytics, hr_tools  # noqa: F401
from app.tools.registry import REGISTRY, ToolContext, execute, tools_for  # noqa: F401
