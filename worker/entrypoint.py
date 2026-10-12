"""Phase 2 worker entrypoint (PLAN.md §24.3). Standard library only.

Reads /io/in/task.json, runs Claude Code headless once, and writes /io/out/result.json.
The stage contract (/io/in/contract.md) tells Claude where to write its outputs:
/io/out/questions.json means it needs a human; the stage's expected outputs mean it finished.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

IN, OUT = Path("/io/in"), Path("/io/out")
TEMPLATE = Path("/opt/sdlc/claude-home")


def seed_config_dir() -> None:
    """Copy the org template (CLAUDE.md + skills library) into the run's CLAUDE_CONFIG_DIR volume.
    Re-synced on every invocation, so skills added to the image reach runs already in progress.
    Sessions and other state in the volume are left alone."""
    cfg = Path(os.environ["CLAUDE_CONFIG_DIR"])
    cfg.mkdir(parents=True, exist_ok=True)
    shutil.copytree(TEMPLATE, cfg, dirs_exist_ok=True)
    (cfg / ".sdlc-seeded").write_text(time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))


def asks_questions(path: Path) -> bool:
    """needs_input only if questions.json really asks something (a model may leave a 'complete' marker)."""
    if not path.exists():
        return False
    try:
        data = json.loads(path.read_text())
    except ValueError:
        return False
    return data.get("status") == "needs_input" and bool(data.get("questions"))


def build_command(task: dict) -> list[str]:
    cmd = ["claude", "-p", task["prompt"], "--output-format", "json",
           "--model", task["model"],
           "--permission-mode", task.get("permission_mode", "dontAsk"),
           "--max-turns", str(task.get("max_turns", 10))]
    if (IN / "contract.md").exists():
        cmd += ["--append-system-prompt-file", str(IN / "contract.md")]
    # Claude Code limits file access to the working dir (/workspace) plus added dirs: the stage's inputs and
    # outputs must be reachable from Bash too (e.g. `cat /io/in/inputs/comments.json`).
    cmd += ["--add-dir", str(IN), "--add-dir", str(OUT)]
    for extra in task.get("add_dirs", []):
        cmd += ["--add-dir", extra]
    if task.get("resume_session"):
        cmd += ["--resume", task["resume_session"]]
    return cmd


def main() -> int:
    task = json.loads((IN / "task.json").read_text())
    OUT.mkdir(parents=True, exist_ok=True)
    seed_config_dir()
    cmd = build_command(task)
    started = time.monotonic()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=task.get("timeout_s", 1200))
        stdout, stderr, code = proc.stdout, proc.stderr, proc.returncode
    except subprocess.TimeoutExpired as exc:
        stdout, stderr, code = exc.stdout or "", f"timeout after {exc.timeout}s", 124
    (OUT / "claude.json").write_text(stdout if isinstance(stdout, str) else stdout.decode())
    (OUT / "claude.stderr").write_text(stderr if isinstance(stderr, str) else stderr.decode())

    try:
        claude = json.loads(stdout)
    except (ValueError, TypeError):
        claude = {}
    expected = task.get("expected_outputs", [])
    if asks_questions(OUT / "questions.json"):
        status = "needs_input"
    elif code == 0 and not claude.get("is_error") and all((OUT / name).exists() for name in expected):
        status = "ok"
    else:
        status = "failed"

    result = {
        "schema_version": "1",
        "status": status,
        "exit_code": code,
        "session_id": claude.get("session_id"),
        "claude_result": (claude.get("result") or "")[:4000],
        "is_error": claude.get("is_error"),
        "num_turns": claude.get("num_turns"),
        "total_cost_usd": claude.get("total_cost_usd"),
        "usage": claude.get("usage"),
        "model_usage": claude.get("modelUsage"),
        "permission_denials": claude.get("permission_denials"),
        "artifacts": sorted(p.name for p in OUT.iterdir() if p.name not in {"result.json"}),
        "duration_s": round(time.monotonic() - started, 1),
        "command": [c if i != 2 else "<prompt>" for i, c in enumerate(cmd)],
    }
    (OUT / "result.json").write_text(json.dumps(result, indent=2))
    print(json.dumps({k: result[k] for k in ("status", "session_id", "num_turns", "total_cost_usd")}))
    return 0 if status != "failed" else 1


if __name__ == "__main__":
    sys.exit(main())
