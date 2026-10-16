from __future__ import annotations

from typing import Any

from app.services.memory_service import memory_service
from app.skills.base import BaseSkill


def _clamp_int(value: Any, *, default: int, minimum: int, maximum: int) -> int:
    try:
        numeric = int(value)
    except (TypeError, ValueError):
        numeric = default
    return max(minimum, min(maximum, numeric))


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class Skill(BaseSkill):
    id = "memory"
    name = "交易记忆"
    description = "长期交易经验的读写工具：形成判断前检索记忆，值得复用的教训写入记忆"
    run_types = None  # 所有 run_type 可用
    tool_run_type_filter: dict[str, set[str]] = {}

    tools = [
        {
            "type": "function",
            "function": {
                "name": "memory_read",
                "description": (
                    "按关键词检索长期交易记忆。形成买入、卖出、调仓、撤单或观望"
                    "判断前，应先查询相关经验（如标的代码、行业、主题、教训关键词）。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "keywords": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "关键词列表（最多 8 个），如 [\"600519\", \"白酒\"]",
                        },
                        "match_mode": {
                            "type": "string",
                            "enum": ["any", "all"],
                            "description": "any=任一关键词命中即返回；all=全部命中才返回",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "返回条数上限（1-10，默认 5）",
                        },
                    },
                    "required": ["keywords"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "memory_write",
                "description": (
                    "写入长期交易记忆：create 新建、update 更新、delete 软删除。"
                    "更新和删除必须带最近读取到的 expected_version，版本冲突时重新读取后再操作。"
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["create", "update", "delete"],
                            "description": "写入动作",
                        },
                        "memory_id": {
                            "type": "integer",
                            "description": "update/delete 时的目标记忆 id",
                        },
                        "content": {
                            "type": "string",
                            "description": "create/update 时的记忆内容（简洁的经验教训）",
                        },
                        "reason": {
                            "type": "string",
                            "description": "形成原因或适用场景",
                        },
                        "expected_version": {
                            "type": "integer",
                            "description": "update/delete 必填：最近读取到的版本号",
                        },
                    },
                    "required": ["action"],
                },
            },
        },
    ]

    def do_memory_read(
        self, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        del context
        keywords = arguments.get("keywords")
        if isinstance(keywords, str):
            keywords = [keywords]
        return memory_service.search_for_agent(
            keywords=keywords,
            match_mode=str(arguments.get("match_mode") or "any"),
            limit=_clamp_int(
                arguments.get("limit"), default=5, minimum=1, maximum=10
            ),
        )

    def do_memory_write(
        self, arguments: dict[str, Any], context: dict[str, Any]
    ) -> dict[str, Any]:
        del context
        return memory_service.write_for_agent(
            action=str(arguments.get("action") or ""),
            memory_id=_optional_int(arguments.get("memory_id")),
            content=arguments.get("content"),
            reason=arguments.get("reason"),
            expected_version=_optional_int(arguments.get("expected_version")),
        )
