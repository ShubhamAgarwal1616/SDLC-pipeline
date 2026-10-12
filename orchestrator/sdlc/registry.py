"""The org repo registry (config/repos.yaml) and the per-run "targets" a plan run works on."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from sdlc.config import HOME, ConfigError

REGISTRY = HOME / "config" / "repos.yaml"


def load(path: Path | None = None) -> dict[str, dict]:
    path = path or REGISTRY
    if not path.exists():
        return {}
    repos = (yaml.safe_load(path.read_text()) or {}).get("repos") or {}
    for name, entry in repos.items():
        for dep in entry.get("depends_on", []) or []:
            if dep not in repos:
                raise ConfigError(f"repos.yaml: {name} depends on unknown repo {dep!r}")
    return repos


def name_for(github_repo: str, repos: dict | None = None) -> str | None:
    repos = load() if repos is None else repos
    return next((n for n, e in repos.items() if e.get("github") == github_repo), None)


def ordered(names: list[str], repos: dict | None = None) -> list[str]:
    """Providers before consumers (topological by depends_on), stable otherwise."""
    repos = load() if repos is None else repos
    out: list[str] = []
    visiting: set[str] = set()

    def visit(n: str) -> None:
        if n in out:
            return
        if n in visiting:
            raise ConfigError(f"repos.yaml: dependency cycle at {n}")
        visiting.add(n)
        for dep in repos.get(n, {}).get("depends_on", []) or []:
            if dep in names:
                visit(dep)
        visiting.discard(n)
        out.append(n)

    for n in names:
        visit(n)
    return out


@dataclass
class Target:
    """One repo a plan run works on. Single-repo runs have one target with name=None and no prefix."""
    name: str | None
    github: str | None
    ws: Path
    publish: Path
    prefix: str = ""
    info: dict = field(default_factory=dict)


def plan_targets(run_dir: Path, inputs: dict) -> list[Target]:
    names = inputs.get("repos")
    if not names:
        return [Target(None, inputs.get("repo"), run_dir / "workspace", run_dir / "publish")]
    repos = load()
    missing = [n for n in names if n not in repos]
    if missing:
        raise ConfigError(f"unknown repos (not in config/repos.yaml): {missing}")
    return [Target(n, repos[n]["github"], run_dir / "workspaces" / n, run_dir / "publish" / n, f"{n}/", repos[n])
            for n in ordered(names, repos)]
