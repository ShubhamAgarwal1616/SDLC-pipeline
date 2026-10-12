"""Offline dependency caches for the sandbox and the verify runner (PLAN.md §25.2), per build stack.

The cache is warmed from the **base branch** (code a human already merged) in a tool container on the egress
network, with no credentials and no LLM. It's keyed by the stack and a hash of its build files, so it's re-warmed
only when they change. Each consumer gets its own copy, so AI-written code running in one container can never
poison the cache another container trusts. Stacks: see sdlc.stacks (Gradle home, npm cache).
"""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from pathlib import Path

from sdlc.config import RUNS
from sdlc.connectors import github
from sdlc.sandbox import SandboxError, SandboxRunner
from sdlc.stacks import GRADLE, Stack

EGRESS_NETWORK = "sdlc_egress"


def cache_key(src: Path, build_files: list[str] | tuple[str, ...]) -> str:
    h = hashlib.sha256()
    for name in sorted(build_files):
        path = src / name
        h.update(name.encode())
        h.update(path.read_bytes() if path.is_file() else b"<missing>")
    return h.hexdigest()[:16]


def cache_root(stack: Stack, repo: str) -> Path:
    # Gradle keeps its pre-0.3 location so caches already warmed (and CI's actions/cache) stay valid.
    sub = "gradle" if stack is GRADLE else f"deps/{stack.name}"
    return RUNS / "_cache" / sub / repo.replace("/", "__")


def warm(runner: SandboxRunner, repo: str, base: str, stack: Stack = GRADLE,
         build_files: list[str] | None = None, timeout_s: int = 1800) -> Path:
    """Return a warmed cache dir for repo@base, building it once if needed."""
    root = cache_root(stack, repo)
    src = root / "src"
    if src.exists():
        shutil.rmtree(src)
    root.mkdir(parents=True, exist_ok=True)
    github.clone(repo, src, branch=base, depth=1)
    home = root / cache_key(src, build_files or stack.build_files)
    if (home / ".complete").exists():
        return home
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    proc = runner.run_tool(src, ["sh", "-c", stack.warm_cmd], writable=True, timeout_s=timeout_s,
                           network=EGRESS_NETWORK, mounts={home: stack.mount},
                           env={k: v for k, v in stack.env.items() if k != "npm_config_offline"})
    (root / "warm.log").write_text(proc.stdout[-20000:] + "\n--- stderr ---\n" + proc.stderr[-20000:])
    if proc.returncode != 0:
        raise SandboxError(f"warming the {stack.name} dependency cache failed (see {root / 'warm.log'}): "
                           f"{proc.stdout.strip()[-400:]}")
    for transient in stack.transient:
        path = home / transient
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    (home / ".complete").write_text("ok\n")
    return home


def seed(warm_home: Path, dest: Path) -> Path:
    """A private, writable copy of the warmed cache."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(["cp", "-R", str(warm_home), str(dest)], check=True)
    return dest
