"""Skill library inspection and lint (PLAN.md §23.5): `sdlc skills list|lint|add`.

Auto-pickup depends on good descriptions, so lint checks that each one says *when* to use the skill.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

import yaml

RESERVED_PREFIX = "sdlc-"
WHEN_HINTS = re.compile(r"\b(use when|use whenever|use this|when the|activate|whenever|used as)\b", re.I)
MAX_SKILL_BYTES = 60_000


@dataclass
class Skill:
    name: str
    folder: str
    description: str
    entry_only: bool          # disable-model-invocation: true
    path: Path


@dataclass
class Finding:
    skill: str
    level: str                # error | warn
    message: str


def parse_skill(skill_md: Path) -> tuple[dict, str]:
    text = skill_md.read_text()
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n?(.*)$", text, re.S)
    if not m:
        return {}, text
    try:
        meta = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        meta = {"__invalid__": True}
    return meta, m.group(2)


def list_skills(root: Path) -> list[Skill]:
    skills = []
    for skill_md in sorted(root.glob("*/SKILL.md")):
        meta, _ = parse_skill(skill_md)
        skills.append(Skill(name=str(meta.get("name") or skill_md.parent.name), folder=skill_md.parent.name,
                            description=" ".join(str(meta.get("description") or "").split()),
                            entry_only=bool(meta.get("disable-model-invocation")), path=skill_md.parent))
    return skills


def lint_dir(root: Path, repo_level: bool, known: set[str] | None = None) -> list[Finding]:
    """repo_level: skills from a project repo (or pack) may not use the reserved sdlc- prefix."""
    if not root.is_dir():
        return []
    skills = list_skills(root)
    names = {s.name for s in skills} | (known or set()) | {s.folder for s in skills}
    findings: list[Finding] = []
    for s in skills:
        meta, body = parse_skill(s.path / "SKILL.md")
        if not meta or meta.get("__invalid__"):
            findings.append(Finding(s.folder, "error", "missing or invalid YAML frontmatter"))
            continue
        if not meta.get("name"):
            findings.append(Finding(s.folder, "error", "frontmatter has no name"))
        elif meta["name"] != s.folder:
            findings.append(Finding(s.name, "warn", f"name '{meta['name']}' differs from folder '{s.folder}' "
                                                    "(the name wins; renaming the folder avoids confusion)"))
        if repo_level and s.name.startswith(RESERVED_PREFIX):
            findings.append(Finding(s.name, "error", f"'{RESERVED_PREFIX}*' names are reserved for core skills"))
        desc = s.description
        if len(desc) < 40:
            findings.append(Finding(s.name, "error", "description is too short for reliable auto-pickup"))
        elif not s.entry_only and not WHEN_HINTS.search(desc):
            findings.append(Finding(s.name, "warn", "description doesn't say when to use it "
                                                    "(e.g. 'Use when …'), which hurts auto-pickup"))
        size = sum(p.stat().st_size for p in s.path.rglob("*") if p.is_file())
        if size > MAX_SKILL_BYTES:
            findings.append(Finding(s.name, "warn", f"{size} bytes; large skills cost context when loaded"))
        for ref in sorted(set(re.findall(r"(?<![\w/])/([a-z][a-z0-9-]{2,})\b", body))):
            if ref not in names and ref not in {"workspace", "io", "tmp", "opt", "run", "etc", "dev", "sdlc"}:
                if re.search(rf"(skill|run a|run the|invoke|use the)\W+`?/{re.escape(ref)}\b", body, re.I):
                    findings.append(Finding(s.name, "warn", f"references /{ref}, which isn't in this library"))
    return findings
