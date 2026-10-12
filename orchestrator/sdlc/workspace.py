"""The run's workspace: the repo the agent works in (mounted at /workspace).

An onboarded project (one with .sdlc/pipeline.yaml in a git repo) is cloned locally.
Otherwise the run gets a fresh scratch repo. Either way OpenSpec is initialised if missing,
in a no-network tool container, never in Phase 1 itself.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sdlc.connectors import github
from sdlc.sandbox import SandboxError, SandboxRunner

GIT_ID = ["-c", "user.name=sdlc-pipeline", "-c", "user.email=sdlc-pipeline@localhost"]


def _git(ws: Path, *args: str) -> None:
    subprocess.run(["git", *GIT_ID, "-C", str(ws), *args], check=True, capture_output=True)


def ensure_workspace(ws: Path, project: Path | None, runner: SandboxRunner, repo: str | None = None) -> Path:
    """`ws` is the workspace dir (normally runs/<id>/workspace; runs/<id>/workspaces/<repo> in feature mode)."""
    if (ws / ".git").exists():
        return ws
    if ws.exists() and any(ws.iterdir()):
        raise SandboxError(f"{ws} exists but isn't a git repo")
    if repo:   # a fresh clone; the token is passed via env only, so it never lands in .git/config
        github.clone(repo, ws, depth=50)
    elif project and (project / ".sdlc" / "pipeline.yaml").exists() and (project / ".git").exists():
        subprocess.run(["git", "clone", "--quiet", "--local", str(project), str(ws)], check=True, capture_output=True)
    else:
        ws.mkdir(parents=True, exist_ok=True)
        _git(ws, "init", "-q")
    if not (ws / "openspec").exists():
        proc = runner.run_tool(ws, ["openspec", "init", "--tools", "claude", "--no-animation", "."], writable=True)
        if proc.returncode != 0:
            raise SandboxError(f"openspec init failed: {proc.stderr.strip()[-400:]}")
    _git(ws, "add", "-A")
    _git(ws, "commit", "-q", "--allow-empty", "-m", "sdlc: workspace baseline")
    return ws
