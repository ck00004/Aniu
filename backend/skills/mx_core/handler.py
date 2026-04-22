from __future__ import annotations

from typing import Any

from app.skills.base import BaseSkill
from skills.mx_core.client import MXClient
from skills.mx_core.execution import mx_execution_service
from skills.mx_core.tool_specs import TOOL_PROFILES


def _load_specs() -> list[dict[str, Any]]:
    return mx_execution_service.build_tools(run_type=None)


class Skill(BaseSkill):
    id = "mx_core"
    name = "妙想核心"
    description = "东方财富妙想 OpenAPI 与 A 股模拟交易工具集"
    run_types = ["analysis", "trade", "chat"]

    def __init__(self) -> None:
        self.tools = _load_specs()
        self.tool_run_type_filter = {}
        for run_type, tool_names in TOOL_PROFILES.items():
            for tool_name in tool_names:
                self.tool_run_type_filter.setdefault(tool_name, set()).add(run_type)

    def handle(self, *, tool_name, arguments, context):
        app_settings = context.get("app_settings")
        client = context.get("client")
        if client is not None:
            return mx_execution_service.execute_tool(
                tool_name=tool_name,
                arguments=arguments,
                client=client,
                app_settings=app_settings,
            )

        with MXClient(api_key=getattr(app_settings, "mx_api_key", None)) as runtime_client:
            return mx_execution_service.execute_tool(
                tool_name=tool_name,
                arguments=arguments,
                client=runtime_client,
                app_settings=app_settings,
            )


# Compatibility export for tests and any lingering imports that still patch the
# old symbol from this module.
mx_skill_service = mx_execution_service
