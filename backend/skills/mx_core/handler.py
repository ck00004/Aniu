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
        # run_id 必须一路透传：交易意图、执行动作回填都要靠它把"这一次运行"
        # 和真实委托关联起来。通用 skill 路径（chat / 非计划执行）过去丢掉了
        # run_id，导致意图的 run_id 为 NULL、审计链断开。
        run_id = context.get("run_id")
        run_id = int(run_id) if isinstance(run_id, int) and run_id > 0 else None
        if client is not None:
            return mx_execution_service.execute_tool(
                tool_name=tool_name,
                arguments=arguments,
                client=client,
                app_settings=app_settings,
                run_id=run_id,
            )

        with MXClient(api_key=getattr(app_settings, "mx_api_key", None)) as runtime_client:
            return mx_execution_service.execute_tool(
                tool_name=tool_name,
                arguments=arguments,
                client=runtime_client,
                app_settings=app_settings,
                run_id=run_id,
            )


# Compatibility export for tests and any lingering imports that still patch the
# old symbol from this module.
mx_skill_service = mx_execution_service
