"""Scan skill directories and load skill packages."""
from __future__ import annotations

import importlib
import importlib.util
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.skills.base import BaseSkill


_FRONTMATTER_RE = re.compile(r"^---\s*\r?\n(.*?)\r?\n---\s*\r?\n?", re.DOTALL)


@dataclass
class SkillPackage:
    id: str
    source: str  # "builtin" | "workspace"
    path: Path
    metadata: dict[str, Any]
    sop_text: str
    skill: BaseSkill | None = None

    @property
    def name(self) -> str:
        return str(self.metadata.get("name") or self.id)

    @property
    def description(self) -> str:
        return str(self.metadata.get("description") or "")

    @property
    def tools(self) -> list[dict[str, Any]]:
        return list(self.skill.tools) if self.skill else []

    def tool_names(self) -> set[str]:
        return self.skill.tool_names() if self.skill else set()

    @property
    def skill_md_path(self) -> Path:
        return self.path / "SKILL.md"

    def _metadata_payloads(self) -> list[dict[str, Any]]:
        payloads: list[dict[str, Any]] = []
        nested = self.metadata.get("metadata")
        if isinstance(nested, dict):
            for key in ("aniu", "openclaw", "nanobot"):
                value = nested.get(key)
                if isinstance(value, dict):
                    payloads.append(value)
        return payloads

    @property
    def run_types(self) -> list[str]:
        if self.skill is not None and getattr(self.skill, "run_types", None):
            run_types = getattr(self.skill, "run_types", None)
            if isinstance(run_types, list):
                return [str(item).strip() for item in run_types if str(item).strip()]

        for payload in [self.metadata, *self._metadata_payloads()]:
            run_types = payload.get("run_types")
            if isinstance(run_types, list):
                normalized = [str(item).strip() for item in run_types if str(item).strip()]
                if normalized:
                    return normalized
        return []

    @property
    def always(self) -> bool:
        for payload in [self.metadata, *self._metadata_payloads()]:
            value = payload.get("always")
            if isinstance(value, bool):
                return value
        return False

    @property
    def requires(self) -> dict[str, list[str]]:
        bins: list[str] = []
        envs: list[str] = []
        for payload in [self.metadata, *self._metadata_payloads()]:
            requires = payload.get("requires")
            if not isinstance(requires, dict):
                continue
            raw_bins = requires.get("bins")
            if isinstance(raw_bins, list):
                bins.extend(str(item).strip() for item in raw_bins if str(item).strip())
            raw_envs = requires.get("env")
            if isinstance(raw_envs, list):
                envs.extend(str(item).strip() for item in raw_envs if str(item).strip())
        return {
            "bins": sorted(set(bins)),
            "env": sorted(set(envs)),
        }

    def supports_run_type(self, run_type: str | None) -> bool:
        normalized = str(run_type or "").strip()
        run_types = self.run_types
        return not normalized or not run_types or normalized in run_types

    def to_info(self, enabled: bool = True) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "source": self.source,
            "enabled": enabled,
            "has_handler": self.skill is not None,
            "tool_names": sorted(self.tool_names()),
        }


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    if not text.startswith("---"):
        return {}, text
    match = _FRONTMATTER_RE.match(text)
    if not match:
        return {}, text
    raw = match.group(1)
    body = text[match.end():]
    meta = _parse_simple_yaml(raw)
    return meta, body.strip()


def _parse_simple_yaml(raw: str) -> dict[str, Any]:
    """Minimal YAML reader for skill frontmatter. Supports flat key: value and
    nested via indent (2 spaces). Values that look like JSON are parsed as JSON."""
    try:
        import yaml  # type: ignore
    except ImportError:
        yaml = None  # fallback parser below

    if yaml is not None:
        try:
            result = yaml.safe_load(raw) or {}
            return result if isinstance(result, dict) else {}
        except Exception:
            pass

    # Fallback: naive key:value parser
    import json

    out: dict[str, Any] = {}
    stack: list[tuple[int, dict[str, Any]]] = [(0, out)]
    for line in raw.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        indent = len(line) - len(line.lstrip())
        while stack and indent < stack[-1][0]:
            stack.pop()
        key, _, value = line.strip().partition(":")
        key = key.strip()
        value = value.strip()
        container = stack[-1][1]
        if not value:
            new_dict: dict[str, Any] = {}
            container[key] = new_dict
            stack.append((indent + 2, new_dict))
            continue
        parsed: Any
        try:
            parsed = json.loads(value)
        except (json.JSONDecodeError, ValueError):
            parsed = value
        container[key] = parsed
    return out


def _load_handler_class(
    skill_dir: Path,
    skill_id: str,
    module_path: str | None,
) -> BaseSkill | None:
    """Import the skill's handler module and return an instantiated BaseSkill
    subclass. Supports both dotted module paths (e.g. "skills.mx_core.handler")
    and a conventional "handler.py" in the skill directory."""
    target_module = None

    if module_path:
        try:
            target_module = importlib.import_module(module_path)
        except ImportError:
            target_module = None

    if target_module is None:
        handler_file = skill_dir / "handler.py"
        if not handler_file.exists():
            return None
        mod_name = f"_aniu_skill_{skill_id}_handler"
        spec = importlib.util.spec_from_file_location(mod_name, handler_file)
        if spec is None or spec.loader is None:
            return None
        target_module = importlib.util.module_from_spec(spec)
        sys.modules[mod_name] = target_module
        try:
            spec.loader.exec_module(target_module)
        except Exception:
            return None

    cls = getattr(target_module, "Skill", None)
    if cls is None:
        for attr_name in dir(target_module):
            attr = getattr(target_module, attr_name)
            if (
                isinstance(attr, type)
                and issubclass(attr, BaseSkill)
                and attr is not BaseSkill
            ):
                cls = attr
                break
    if cls is None:
        return None
    instance = cls()
    # Keep the folder name as the canonical internal id so that enable/disable
    # state and workspace imports stay stable even when metadata.name changes.
    instance.id = skill_id
    return instance


def _scan_dir(base: Path, source: str) -> list[SkillPackage]:
    if not base.exists() or not base.is_dir():
        return []
    packages: list[SkillPackage] = []
    for child in sorted(base.iterdir()):
        if not child.is_dir():
            continue
        skill_file = child / "SKILL.md"
        if not skill_file.exists():
            continue
        try:
            content = skill_file.read_text(encoding="utf-8")
        except OSError:
            continue
        metadata, body = _parse_frontmatter(content)
        skill_id = child.name
        aniu_meta = (metadata.get("metadata") or {}).get("aniu") or {}
        handler_module = aniu_meta.get("handler_module")
        skill = _load_handler_class(child, skill_id, handler_module)
        packages.append(
            SkillPackage(
                id=skill_id,
                source=source,
                path=child,
                metadata=metadata,
                sop_text=body,
                skill=skill,
            )
        )
    return packages


def discover_skill_packages(
    builtin_dir: Path | None = None,
    workspace_dir: Path | None = None,
) -> list[SkillPackage]:
    """Return merged skill packages from builtin then workspace, workspace-wins on id collision."""
    pkgs: dict[str, SkillPackage] = {}
    if builtin_dir is not None:
        for p in _scan_dir(builtin_dir, "builtin"):
            pkgs[p.id] = p
    if workspace_dir is not None:
        for p in _scan_dir(workspace_dir, "workspace"):
            pkgs[p.id] = p
    return list(pkgs.values())
