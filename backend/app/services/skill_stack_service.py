from __future__ import annotations

import os
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.skills.loader import SkillPackage
from app.skills.registry import skill_registry

_IGNORED_SUPPORT_FILES = {
    "SKILL.md",
    "_meta.json",
    "__pycache__",
}
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


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        import json

        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _list_support_files(pkg: SkillPackage) -> list[str]:
    files: list[str] = []
    for path in pkg.path.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(pkg.path).as_posix()
        if relative in _IGNORED_SUPPORT_FILES or relative.startswith("__pycache__/"):
            continue
        files.append(relative)
    return sorted(files)[:12]


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


class SkillPolicyProvider:
    def layer(self, pkg: SkillPackage) -> str:
        return "runtime" if skill_registry.is_system_runtime(pkg) else "standard"

    def can_toggle(self, pkg: SkillPackage) -> bool:
        return not skill_registry.is_system_runtime(pkg)

    def can_delete(self, pkg: SkillPackage) -> bool:
        return str(pkg.source or "") == "workspace"

    def summary(self, pkg: SkillPackage) -> tuple[str, str]:
        if skill_registry.is_system_runtime(pkg):
            return (
                "运行时底座",
                "为所有技能提供共享执行能力，始终启用，不允许停用或删除。",
            )
        if str(pkg.source or "") == "builtin":
            return ("系统技能", "支持启停管理，但不允许删除。")
        return ("工作区技能", "支持启停管理，也允许从工作区删除。")


class SkillCatalogProvider:
    def __init__(self, policy: SkillPolicyProvider) -> None:
        self._policy = policy

    def _extract_category(self, pkg: SkillPackage) -> str | None:
        meta = pkg.metadata.get("metadata")
        if not isinstance(meta, dict):
            return None
        for key in ("aniu", "openclaw"):
            payload = meta.get(key)
            if isinstance(payload, dict):
                category = payload.get("category")
                if isinstance(category, str) and category.strip():
                    return category.strip()
        return None

    def _extract_run_types(self, pkg: SkillPackage) -> list[str]:
        return list(getattr(pkg, "run_types", []) or [])

    def _build_compatibility(self, pkg: SkillPackage) -> tuple[str, str, list[str]]:
        if skill_registry.is_system_runtime(pkg):
            return "native", "System runtime tools supporting all other skills.", []

        issues: list[str] = []
        requires = getattr(pkg, "requires", {}) or {}
        bins = requires.get("bins") if isinstance(requires.get("bins"), list) else []
        envs = requires.get("env") if isinstance(requires.get("env"), list) else []

        if bins:
            issues.append(
                "Declared external binary dependencies: "
                + ", ".join(str(item) for item in bins if str(item).strip())
                + "; please ensure these commands are available before execution."
            )
        if envs:
            issues.append(
                "Declared environment variable dependencies: "
                + ", ".join(str(item) for item in envs if str(item).strip())
                + "; please configure these variables before execution."
            )

        if pkg.skill is not None and not issues:
            return "native", "Native Aniu skill with directly callable tools.", issues
        if pkg.skill is not None:
            return (
                "needs_attention",
                "Skill can be loaded, but there are extra runtime prerequisites to verify.",
                issues,
            )
        if issues:
            return (
                "needs_attention",
                "Shared-runtime skill is supported, but runtime prerequisites still need attention.",
                issues,
            )
        return (
            "prompt_only",
            "Shared-runtime skill that is executed from SKILL.md guidance.",
            issues,
        )

    def build_skill_list_item(self, pkg: SkillPackage, *, enabled: bool) -> dict[str, Any]:
        policy_label, policy_summary = self._policy.summary(pkg)
        return {
            "id": pkg.id,
            "name": pkg.name,
            "description": pkg.description,
            "source": pkg.source,
            "enabled": enabled,
            "layer": self._policy.layer(pkg),
            "can_toggle": self._policy.can_toggle(pkg),
            "can_delete": self._policy.can_delete(pkg),
            "policy_label": policy_label,
            "policy_summary": policy_summary,
        }

    def build_skill_info(self, pkg: SkillPackage, *, enabled: bool) -> dict[str, Any]:
        meta_payload = _read_json_file(pkg.path / "_meta.json")
        compatibility_level, compatibility_summary, issues = self._build_compatibility(pkg)
        published_at = meta_payload.get("publishedAt")
        published_at_value = None
        if isinstance(published_at, (int, float)) and published_at > 0:
            published_at_value = datetime.fromtimestamp(published_at / 1000, tz=UTC)

        clawhub_slug = meta_payload.get("slug")
        clawhub_slug_value = (
            str(clawhub_slug).strip()
            if isinstance(clawhub_slug, str) and clawhub_slug.strip()
            else None
        )
        source_url = meta_payload.get("source_url")
        source_url_value = (
            str(source_url).strip()
            if isinstance(source_url, str) and source_url.strip()
            else None
        )
        return {
            **self.build_skill_list_item(pkg, enabled=enabled),
            "location": str(pkg.path.resolve()),
            "has_handler": pkg.skill is not None,
            "tool_names": sorted(pkg.tool_names()),
            "run_types": self._extract_run_types(pkg),
            "category": self._extract_category(pkg),
            "compatibility_level": compatibility_level,
            "compatibility_summary": compatibility_summary,
            "issues": issues,
            "support_files": _list_support_files(pkg),
            "clawhub_slug": clawhub_slug_value,
            "clawhub_version": (
                str(meta_payload.get("version")).strip()
                if meta_payload.get("version") is not None
                else None
            ),
            "clawhub_url": (
                source_url_value
                if source_url_value
                else (
                    f"https://clawhub.ai/skills/{clawhub_slug_value}"
                    if clawhub_slug_value
                    else None
                )
            ),
            "published_at": published_at_value,
        }


class SkillRuntimeProvider:
    def build_tools(self, *, run_type: str | None = None) -> list[dict[str, Any]]:
        rt = str(run_type or "analysis").strip() or "analysis"
        collected: list[dict[str, Any]] = []
        seen_names: set[str] = set()
        packages = sorted(
            skill_registry.enabled_packages(),
            key=lambda pkg: (
                0 if skill_registry.is_system_runtime(pkg) else 1,
                pkg.name.lower(),
            ),
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
        for pkg in skill_registry.enabled_packages():
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

    def runtime_tool_names(self, *, run_type: str | None = None) -> list[str]:
        normalized = str(run_type or "analysis").strip() or "analysis"
        tool_names = {
            spec.get("function", {}).get("name", "")
            for pkg in skill_registry.enabled_packages()
            if skill_registry.is_system_runtime(pkg)
            and pkg.skill is not None
            and pkg.supports_run_type(normalized)
            for spec in pkg.skill.tools_for(normalized)
            if spec.get("function", {}).get("name")
        }
        ordered = [name for name in _PREFERRED_RUNTIME_TOOL_ORDER if name in tool_names]
        extras = sorted(tool_names - set(ordered))
        return ordered + extras


class SkillContextProvider:
    def __init__(self, runtime: SkillRuntimeProvider) -> None:
        self._runtime = runtime

    def _packages_for_prompt(self, run_type: str) -> list[SkillPackage]:
        return [
            pkg
            for pkg in skill_registry.enabled_packages()
            if not skill_registry.is_system_runtime(pkg) and pkg.supports_run_type(run_type)
        ]

    def _build_skill_summary_line(self, pkg: SkillPackage) -> str:
        parts = [f"入口: `{pkg.skill_md_path.resolve()}`"]
        if pkg.description:
            parts.insert(0, pkg.description)
        missing = _format_missing_requirements(pkg)
        if missing:
            parts.append(missing)
        return f"- **{pkg.name}**（`{pkg.id}`）：" + "；".join(parts)

    def build_prompt_supplement(self, *, run_type: str | None = None) -> str:
        rt = str(run_type or "analysis").strip() or "analysis"
        parts: list[str] = []

        runtime_tools = self._runtime.runtime_tool_names(run_type=rt)
        if runtime_tools:
            parts.append(
                "\n".join(
                    [
                        "## 技能运行时",
                        "以下共享运行时工具始终可用："
                        + ", ".join(f"`{name}`" for name in runtime_tools),
                        "优先使用运行时工具与对应 SKILL.md 配合完成任务。",
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
                "## 已启用技能目录",
                "需要使用某个技能时，先阅读对应 `SKILL.md`：",
            ]
            summary_lines.extend(
                self._build_skill_summary_line(pkg)
                for pkg in summary_packages
            )
            parts.append("\n".join(summary_lines))

        return "\n\n".join(part for part in parts if part.strip())


class SkillStackService:
    def __init__(self) -> None:
        self.policy = SkillPolicyProvider()
        self.catalog = SkillCatalogProvider(self.policy)
        self.runtime = SkillRuntimeProvider()
        self.context = SkillContextProvider(self.runtime)


skill_stack_service = SkillStackService()