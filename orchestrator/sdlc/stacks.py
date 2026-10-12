"""Build stacks the pipeline supports: how to detect one, fill its offline dependency cache, build and test it,
and spot a new dependency. Everything stack-specific lives here; the stages ask for the repo's stack.

- gradle: Kotlin/JVM (Gradle wrapper + build.gradle.kts). Cache: GRADLE_USER_HOME.
- node:   Node.js/TypeScript (package.json + package-lock.json). Cache: npm's cache, used with --offline.
A repo can pin one with `project.stack` in .sdlc/pipeline.yaml; otherwise it's detected from the files.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path


@dataclass(frozen=True)
class Stack:
    name: str
    markers: tuple[str, ...]                 # all must exist for detection
    build_files: tuple[str, ...]             # their hash keys the dependency cache
    warm_cmd: str                            # fills the cache from the base branch (egress tool container, no LLM)
    mount: str                               # where the cache is mounted in containers
    env: dict = field(default_factory=dict)  # makes the build use (only) that cache
    verify: tuple[str, ...] = ()             # default verify commands (offline)
    dependency_files: tuple[str, ...] = ()   # files the diff guard checks for new dependencies
    skill_hints: tuple[dict, ...] = ()
    transient: tuple[str, ...] = ()          # cache subdirs dropped after warming
    compile_error: str = r"^$"               # a log line that is a compile error


GRADLE = Stack(
    name="gradle",
    markers=("gradlew", "build.gradle.kts"),
    build_files=("build.gradle.kts", "settings.gradle.kts", "gradle/wrapper/gradle-wrapper.properties"),
    warm_cmd="./gradlew --no-daemon --console=plain build",
    mount="/run/gradle",
    env={"GRADLE_USER_HOME": "/run/gradle"},
    verify=("./gradlew --offline --no-daemon --console=plain build",),
    dependency_files=("build.gradle.kts", "build.gradle", "gradle/libs.versions.toml"),
    skill_hints=({"paths": ["**/*.kt"], "skill": "kotlin-service-standards"},),
    transient=("daemon", "native", ".tmp"),
    compile_error=r"^e: ",
)

NODE = Stack(
    name="node",
    markers=("package.json", "package-lock.json"),
    build_files=("package.json", "package-lock.json"),
    # --ignore-scripts: no install scripts run while the cache is filled (supply-chain hygiene)
    warm_cmd="npm ci --ignore-scripts --no-audit --no-fund --cache /run/npm",
    mount="/run/npm",
    env={"npm_config_cache": "/run/npm", "npm_config_offline": "true", "npm_config_audit": "false",
         "npm_config_fund": "false", "npm_config_update_notifier": "false", "npm_config_ignore_scripts": "true"},
    verify=("npm ci", "npm run lint --if-present", "npm test"),
    dependency_files=("package.json", "package-lock.json"),
    transient=("_logs", "_update-notifier-last-checked"),
    compile_error=r".*error TS\d+:",
)

STACKS = {s.name: s for s in (GRADLE, NODE)}


def detect(ws: Path, pinned: str | None = None) -> Stack | None:
    if pinned:
        if pinned not in STACKS:
            raise ValueError(f"unknown project.stack {pinned!r}; supported: {sorted(STACKS)}")
        return STACKS[pinned]
    return next((s for s in STACKS.values() if all((ws / m).exists() for m in s.markers)), None)


# ---- new dependencies (the diff guard) ---------------------------------------------------------------------

GRADLE_DEP = re.compile(r"^\s*(implementation|api|testImplementation|runtimeOnly|compileOnly|kapt|ksp|"
                        r"testRuntimeOnly|developmentOnly)\s*\(")
NODE_SECTIONS = ("dependencies", "devDependencies", "optionalDependencies", "peerDependencies")


def _gradle_deps(text: str) -> set[str]:
    return {line.strip() for line in text.splitlines() if GRADLE_DEP.match(line)}


def _package_json_deps(text: str) -> set[str]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return set()
    return {f"{section}: {name}" for section in NODE_SECTIONS for name in (data.get(section) or {})}


def _lockfile_packages(text: str) -> set[str]:
    try:
        data = json.loads(text or "{}")
    except ValueError:
        return set()
    return {p.split("node_modules/")[-1] for p in (data.get("packages") or {}) if p}


def added_dependencies(path: str, before: str, after: str) -> list[str]:
    """Dependencies `after` declares that `before` didn't, for any supported build file."""
    name = Path(path).name
    if name == "package.json":
        return sorted(_package_json_deps(after) - _package_json_deps(before))
    if name == "package-lock.json":
        return [f"lockfile: {p}" for p in sorted(_lockfile_packages(after) - _lockfile_packages(before))]
    return [line for line in (ln.strip() for ln in after.splitlines()) if GRADLE_DEP.match(line)
            and line not in _gradle_deps(before)]


DEPENDENCY_FILES = tuple(sorted({f for s in STACKS.values() for f in s.dependency_files} | {"pom.xml"}))
