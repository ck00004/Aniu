"""Global registry that aggregates enabled skill packages."""
from __future__ import annotations

import os
import shutil
import threading
from pathlib import Path
from typing import Any

from app.skills.loader import SkillPackage, discover_skill_packages


# Built-in skills ship with the backend under backend/skills/
_BACKEND_DIR = Path(__file__).resolve().parents[2]
_DEFAULT_BUILTIN_DIR = _BACKEND_DIR / "skills"
_SYSTEM_RUNTIME_IDS = {"builtin_utils"}
_IGNORED_SUPPORT_FILES = {"SKILL.md", "_meta.json"}
_PREFERRED_RUNTIME_TOOL_ORDER = [
    "read_file",
    "write_file",
    "edit_file",
    "list_dir",
    "glob",
    "grep",
    "exec",
    "web_search",
    "web_fetch",
    "http_get",
    "http_post",
]


def _default_workspace_dir() -> Path:
    try:
        from app.core.config import get_skill_workspace_skills_dir, get_settings

        return get_skill_workspace_skills_dir(get_settings())
    except Exception:
        return _BACKEND_DIR / "data" / "skill_workspace" / "skills"


def _truncate_items(values: list[str], *, limit: int = 4) -> list[str]:
    items = [str(value).strip() for value in values if str(value).strip()]
    if len(items) <= limit:
        return items
    return [*items[:limit], f"...(+{len(items) - limit})"]


def _list_support_files(pkg: SkillPackage, *, limit: int = 4) -> list[str]:
    files: list[str] = []
    for path in pkg.path.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(pkg.path).as_posix()
        if relative in _IGNORED_SUPPORT_FILES or relative.startswith("__pycache__/"):
            continue
        files.append(relative)
    return _truncate_items(sorted(files), limit=limit)


def _format_missing_requirements(pkg: SkillPackage) -> str | None:
    requires = pkg.requires
    missing_bins = [
        item for item in requires.get("bins", []) if shutil.which(item) is None
    ]
    missing_envs = [
        item for item in requires.get("env", []) if not os.environ.get(item)
    ]
    details: list[str] = []
    if missing_bins:
        details.append("缺少命令: " + ", ".join(missing_bins))
    if missing_envs:
        details.append("缺少环境变量: " + ", ".join(missing_envs))
    return "; ".join(details) if details else None


class SkillRegistry:
    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._packages: list[SkillPackage] = []
        self._disabled: set[str] = set()
        self._loaded = False

    def ensure_loaded(self) -> None:
        with self._lock:
            if self._loaded:
                return
            self.reload()

    def reload(
        self,
        builtin_dir: Path | None = None,
        workspace_dir: Path | None = None,
    ) -> None:
        with self._lock:
            self._packages = discover_skill_packages(
                builtin_dir=builtin_dir or _DEFAULT_BUILTIN_DIR,
                workspace_dir=workspace_dir or _default_workspace_dir(),
            )
            self._loaded = True

    def is_system_runtime(self, skill_id: str | SkillPackage) -> bool:
        target_id = skill_id.id if isinstance(skill_id, SkillPackage) else str(skill_id)
        return target_id in _SYSTEM_RUNTIME_IDS

    def set_disabled(self, skill_ids: set[str]) -> None:
        with self._lock:
            self._disabled = {
                skill_id for skill_id in skill_ids if skill_id not in _SYSTEM_RUNTIME_IDS
            }

    def _is_enabled(self, pkg: SkillPackage) -> bool:
        return self.is_system_runtime(pkg) or pkg.id not in self._disabled

    def enabled_packages(self) -> list[SkillPackage]:
        self.ensure_loaded()
        with self._lock:
            return [p for p in self._packages if self._is_enabled(p)]

    def all_packages(self) -> list[SkillPackage]:
        self.ensure_loaded()
        with self._lock:
            return list(self._packages)

    def _packages_for_prompt(self, run_type: str | None) -> list[SkillPackage]:
        normalized = str(run_type or "").strip()
        return [
            pkg
            for pkg in self.enabled_packages()
            if not self.is_system_runtime(pkg) and pkg.supports_run_type(normalized)
        ]

    def _runtime_tool_names(self, run_type: str | None) -> list[str]:
        normalized = str(run_type or "").strip() or "analysis"
        tool_names = {
            spec.get("function", {}).get("name", "")
            for pkg in self.enabled_packages()
            if self.is_system_runtime(pkg)
            and pkg.skill is not None
            and pkg.supports_run_type(normalized)
            for spec in pkg.skill.tools_for(normalized)
            if spec.get("function", {}).get("name")
        }
        ordered = [name for name in _PREFERRED_RUNTIME_TOOL_ORDER if name in tool_names]
        extras = sorted(tool_names - set(ordered))
        return ordered + extras

    def _skill_mode_label(self, pkg: SkillPackage, run_type: str) -> str:
        if pkg.skill is not None:
            tool_names = [
                spec.get("function", {}).get("name", "")
                for spec in pkg.skill.tools_for(run_type)
                if spec.get("function", {}).get("name")
            ]
            if tool_names:
                return "原生工具: " + ", ".join(_truncate_items(tool_names))
        return "文档驱动: 读取 `SKILL.md` 后配合运行时工具执行"

    def _build_skill_summary_line(
        self,
        pkg: SkillPackage,
        *,
        run_type: str,
    ) -> str:
        details = [
            self._skill_mode_label(pkg, run_type),
            f"入口: `{pkg.skill_md_path.resolve()}`",
        ]
        missing = _format_missing_requirements(pkg)
        details.append(
            f"状态: 当前不可直接执行（{missing}）" if missing else "状态: 当前可用"
        )
        support_files = _list_support_files(pkg)
        if support_files:
            details.append(
                "支持文件: " + ", ".join(f"`{name}`" for name in support_files)
            )
        description = pkg.description or "无额外描述"
        return (
            f"- **{pkg.name}**（`{pkg.id}`）- {description}；"
            + "；".join(details)
        )

    def build_tools(self, *, run_type: str | None = None) -> list[dict[str, Any]]:
        rt = str(run_type or "analysis").strip() or "analysis"
        collected: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        packages = sorted(
            self.enabled_packages(),
            key=lambda pkg: (0 if self.is_system_runtime(pkg) else 1, pkg.name.lower()),
        )
        for pkg in packages:
            if pkg.skill is None or not pkg.supports_run_type(rt):
                continue
            for spec in pkg.skill.tools_for(rt):
                name = spec.get("function", {}).get("name")
                if not name or name in seen_names:
                    continue
                seen_names.add(name)
                collected.append(spec)
        return collected

    def execute_tool(
        self,
        *,
        tool_name: str,
        arguments: dict[str, Any],
        context: dict[str, Any],
    ) -> dict[str, Any]:
        for pkg in self.enabled_packages():
            if pkg.skill is None:
                continue
            if tool_name in pkg.skill.tool_names():
                try:
                    return pkg.skill.handle(
                        tool_name=tool_name,
                        arguments=arguments,
                        context=context,
                    )
                except Exception as exc:  # noqa: BLE001
                    return {
                        "ok": False,
                        "tool_name": tool_name,
                        "error": str(exc),
                    }
        return {
            "ok": False,
            "tool_name": tool_name,
            "error": f"未知工具调用: {tool_name}",
        }

    def build_prompt_supplement(self, *, run_type: str | None = None) -> str:
        """Build runtime guidance, always-on skills, and enabled skill summaries."""
        rt = str(run_type or "analysis").strip() or "analysis"
        parts: list[str] = []

        runtime_tools = self._runtime_tool_names(rt)
        if runtime_tools:
            parts.append(
                "\n".join(
                    [
                        "## 技能运行时",
                        "以下通用运行时工具始终可用，可作为所有技能的执行底座："
                        + ", ".join(f"`{name}`" for name in runtime_tools),
                        "`read_file` 只适用于纯文本文件；不要对 PDF、图片、docx/xlsx/pptx 等二进制附件调用 `read_file`。",
                        "处理大文件或多参考文件时，优先使用 `glob` / `grep` 缩小范围，再读取具体文件。",
                        "所有写入与命令执行仅允许在 skill workspace 内进行；内置技能文档可以读取，但不能修改。",
                    ]
                )
            )

        prompt_packages = self._packages_for_prompt(rt)
        always_packages = [pkg for pkg in prompt_packages if pkg.always and pkg.sop_text]
        summary_packages = [pkg for pkg in prompt_packages if not pkg.always]

        if always_packages:
            parts.append(
                "\n\n".join(
                    f"## 常驻技能：{pkg.name}\n{pkg.sop_text}"
                    for pkg in always_packages
                )
            )

        if summary_packages:
            summary_lines = [
                "## 已启用技能摘要",
                "需要使用某个技能时，请先定位对应 `SKILL.md`：",
            ]
            summary_lines.extend(
                self._build_skill_summary_line(pkg, run_type=rt)
                for pkg in summary_packages
            )
            parts.append("\n".join(summary_lines))

        return "\n\n".join(part for part in parts if part.strip())

    def list_skill_info(self) -> list[dict[str, Any]]:
        return [
            pkg.to_info(enabled=self._is_enabled(pkg))
            for pkg in self.all_packages()
        ]


skill_registry = SkillRegistry()
