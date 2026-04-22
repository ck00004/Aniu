"""Base classes for skill packs."""
from __future__ import annotations

from typing import Any


class BaseSkill:
    """Subclass this to register a structured skill pack.

    Required attributes on subclass:
        id: Stable identifier, matches the folder name under backend/skills/.
        name: Human-readable display name.
        description: One-line summary.
        tools: List of OpenAI function-format tool specs.

    Optional:
        run_types: Which run_type values this skill applies to.
            None or empty means all. Defaults to None (all).
        tool_run_type_filter: dict[tool_name, set[run_type]] for per-tool
            gating. Tool names not in the dict are available to all run types.
    """

    id: str = ""
    name: str = ""
    description: str = ""
    tools: list[dict[str, Any]] = []
    run_types: list[str] | None = None
    tool_run_type_filter: dict[str, set[str]] = {}

    def tools_for(self, run_type: str) -> list[dict[str, Any]]:
        if self.run_types and run_type not in self.run_types:
            return []
        out: list[dict[str, Any]] = []
        for spec in self.tools:
            name = spec.get("function", {}).get("name")
            allowed = self.tool_run_type_filter.get(name)
            if allowed and run_type not in allowed:
                continue
            out.append(spec)
        return out

    def tool_names(self) -> set[str]:
        return {spec["function"]["name"] for spec in self.tools if spec.get("function")}

    def handle(self, *, tool_name: str, arguments: dict[str, Any], context: dict[str, Any]) -> dict[str, Any]:
        method = getattr(self, f"do_{tool_name}", None)
        if method is None:
            return {
                "ok": False,
                "tool_name": tool_name,
                "error": f"技能 {self.id} 未实现工具 {tool_name}",
            }
        return method(arguments=arguments, context=context)
