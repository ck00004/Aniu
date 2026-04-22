from __future__ import annotations

"""Compatibility shim for legacy imports.

The canonical mx-core implementation now lives under `skills.mx_core` so the
skill package owns its runtime scripts and helper utilities. Keep this module as
an import-stable facade until callers are migrated.
"""

from skills.mx_core.client import MXClient
from skills.mx_core.parsers import (
    extract_available_balance,
    extract_candidates,
    extract_position_symbols,
)

__all__ = [
    "MXClient",
    "extract_available_balance",
    "extract_candidates",
    "extract_position_symbols",
]
