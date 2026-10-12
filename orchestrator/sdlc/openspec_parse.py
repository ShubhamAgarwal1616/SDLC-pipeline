"""Read OpenSpec change folders without an LLM: proposal sections, scenarios and tasks."""

from __future__ import annotations

import re
from pathlib import Path


def sections(markdown: str) -> dict[str, str]:
    out: dict[str, str] = {}
    current = None
    for line in markdown.splitlines():
        if m := re.match(r"^##\s+(.*)", line):
            current = m.group(1).strip()
            out[current] = ""
        elif current is not None:
            out[current] += line + "\n"
    return {k: v.strip() for k, v in out.items()}


def scenarios(change_dir: Path) -> list[dict]:
    found = []
    for spec in sorted(change_dir.glob("specs/**/spec.md")):
        requirement = None
        current = None
        for line in spec.read_text().splitlines():
            if m := re.match(r"^###\s+Requirement:\s*(.*)", line):
                requirement = m.group(1).strip()
            elif m := re.match(r"^####\s+Scenario:\s*(.*)", line):
                current = {"requirement": requirement, "name": m.group(1).strip(), "steps": []}
                found.append(current)
            elif current and (m := re.match(r"^\s*-\s+\*\*(WHEN|THEN|AND|GIVEN)\*\*\s*(.*)", line)):
                current["steps"].append(f"{m.group(1)} {m.group(2).strip()}")
    return found


def tasks(change_dir: Path) -> list[tuple[bool, str]]:
    path = change_dir / "tasks.md"
    if not path.exists():
        return []
    return [(m.group(1).lower() == "x", m.group(2).strip())
            for m in re.finditer(r"^\s*-\s+\[([ xX])\]\s+(.*)$", path.read_text(), re.M)]


def story_markdown(change_id: str, change_dir: Path, spec_url: str) -> str:
    proposal = sections((change_dir / "proposal.md").read_text()) if (change_dir / "proposal.md").exists() else {}
    parts = []
    for heading in ("Why", "What Changes", "Impact"):
        if proposal.get(heading):
            parts.append(f"## {heading}\n{proposal[heading]}")
    acs = [f"- **{s['name']}**: " + "; ".join(s["steps"]) for s in scenarios(change_dir)]
    if acs:
        parts.append("## Acceptance criteria\n" + "\n".join(acs))
    todo = [f"- [{'x' if done else ' '}] {text}" for done, text in tasks(change_dir)]
    if todo:
        parts.append("## Tasks\n" + "\n".join(todo))
    parts.append(f"## Spec\n[openspec/changes/{change_id}]({spec_url})\n\nopenspec-change: `{change_id}`")
    return "\n\n".join(parts)
