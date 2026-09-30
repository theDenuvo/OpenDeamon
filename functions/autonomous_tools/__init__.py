"""
Autonomous Tools — Skill Pack for OpenDeamon

Auto-discover or create missing capabilities.
"""

from .discover import discover
from .create import create_tool
from .verify import verify
from .register import register_tool, rollback_registration
from .__main__ import autonomous_tool

__all__ = [
    "discover",
    "create_tool",
    "verify",
    "register_tool",
    "rollback_registration",
    "autonomous_tool"
]

__version__ = "0.1.0"