from __future__ import annotations

"""Compatibility shim for the legacy mx skill service path.

The canonical mx-core tool specs and execution logic now live under
`skills.mx_core`. Keep this facade so existing service imports and tests keep
working while the rest of the codebase migrates to the skill-local modules.
"""

from skills.mx_core.execution import ERROR_HINTS, mx_execution_service
from skills.mx_core.tool_specs import MXToolSpec, TOOL_PROFILES

_ERROR_HINTS = ERROR_HINTS
_TOOL_PROFILES = TOOL_PROFILES
MXSkillService = type(mx_execution_service)
mx_skill_service = mx_execution_service

__all__ = [
    "_ERROR_HINTS",
    "_TOOL_PROFILES",
    "MXSkillService",
    "MXToolSpec",
    "mx_skill_service",
]
