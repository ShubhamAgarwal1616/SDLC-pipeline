"""Start Phase 2 containers from Phase 1 (PLAN.md §24, §25.2).

- run_agent: an agent sandbox. Claude Code headless, network = the internal network (gateway only).
- run_tool:  a tool container (OpenSpec CLI, later the verify runner). Network none, no LLM, no key.
Both run as the image's non-root user with all capabilities dropped. Phase 1 never runs repo code itself.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

from sdlc import gateway_keys
from sdlc.infra import user_args
from sdlc.config import EffectiveConfig, expand


class SandboxError(Exception):
    """The sandbox could not run, or the agent failed."""


@dataclass
class SandboxTask:
    stage: str
    prompt: str
    model: str
    max_turns: int = 20
    contract: Path | None = None
    inputs: dict[str, Path] = field(default_factory=dict)
    expected_outputs: list[str] = field(default_factory=list)
    resume_session: str | None = None
    add_dirs: list[str] = field(default_factory=list)
    timeout_s: int = 1200
    mounts: dict[Path, str] = field(default_factory=dict)    # extra host path → container path[:ro]
    workspace: Path | None = None        # default: <run_dir>/workspace (the agent's repo)
    workspace_readonly: bool = False
    env: dict[str, str] = field(default_factory=dict)


@dataclass
class SandboxResult:
    status: str               # ok | needs_input | failed
    out_dir: Path
    data: dict
    usage: dict

    def file(self, name: str) -> Path:
        return self.out_dir / name

    def failure(self) -> str:
        stderr = self.file("claude.stderr")
        tail = stderr.read_text()[-600:] if stderr.exists() else ""
        return (self.data.get("claude_result") or tail or "no output").strip()[:800]


HARDENING = ["--cap-drop", "ALL", "--security-opt", "no-new-privileges"]


class SandboxRunner:
    def __init__(self, cfg: EffectiveConfig):
        self.cfg = cfg
        self.image = expand(cfg.get("sandbox.image", "sdlc-worker:dev"))
        self.network = cfg.get("sandbox.docker_network", "sdlc_internal")
        self.gateway_url = cfg.get("sandbox.gateway_url", "http://llm-gateway:4000")
        self.health_url = cfg.get("sandbox.gateway_health_url", "http://127.0.0.1:4000/health")
        self.limits = ["--cpus", str(cfg.get("sandbox.cpus", 2)), "--memory", str(cfg.get("sandbox.memory", "4g")),
                       "--pids-limit", str(cfg.get("sandbox.pids_limit", 512))]

    def preflight(self, need_gateway: bool = True) -> None:
        """Pull images, create networks and start the gateway if needed (a fresh machine or CI runner)."""
        from sdlc import infra
        try:
            infra.ensure(self.cfg, need_gateway)
        except Exception as exc:   # InfraError, or docker itself missing
            raise SandboxError(str(exc)) from exc

    def run_agent(self, run_dir: Path, task: SandboxTask, key: str, io_dir: Path,
                  run_id: str | None = None) -> SandboxResult:
        inn, out = io_dir / "in", io_dir / "out"
        (inn / "inputs").mkdir(parents=True, exist_ok=True)
        out.mkdir(parents=True, exist_ok=True)
        spec = {"prompt": task.prompt, "model": task.model, "max_turns": task.max_turns,
                "expected_outputs": task.expected_outputs, "timeout_s": task.timeout_s,
                "add_dirs": task.add_dirs, "resume_session": task.resume_session}
        (inn / "task.json").write_text(json.dumps(spec, indent=2))
        if task.contract:
            shutil.copy(task.contract, inn / "contract.md")
        for name, src in task.inputs.items():
            shutil.copy(src, inn / "inputs" / name)
        for sub in ("claude-home", "workspace"):
            (run_dir / sub).mkdir(exist_ok=True)

        cmd = ["docker", "run", "--rm", "--network", self.network, *HARDENING, *self.limits, *user_args(),
               "-e", f"ANTHROPIC_BASE_URL={self.gateway_url}", "-e", f"ANTHROPIC_AUTH_TOKEN={key}",
               "-e", "ANTHROPIC_API_KEY=",
               "-v", f"{inn}:/io/in:ro", "-v", f"{out}:/io/out",
               "-v", f"{run_dir / 'claude-home'}:/run/claude",
               "-v", f"{task.workspace or run_dir / 'workspace'}:/workspace{':ro' if task.workspace_readonly else ''}",
               *[arg for host, inside in task.mounts.items() for arg in ("-v", f"{host}:{inside}")],
               *[arg for k, v in task.env.items() for arg in ("-e", f"{k}={v}")],
               self.image]
        offset = gateway_keys.usage_offset()
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=task.timeout_s + 120)
        except subprocess.TimeoutExpired as exc:
            raise SandboxError(f"{task.stage}: sandbox timed out after {exc.timeout}s") from exc
        (io_dir / "docker.log").write_text(proc.stdout[-4000:] + "\n--- stderr ---\n" + proc.stderr[-4000:])
        result_file = out / "result.json"
        if not result_file.exists():
            raise SandboxError(f"{task.stage}: no result.json from the sandbox: {proc.stderr.strip()[-500:]}")
        data = json.loads(result_file.read_text())
        usage = gateway_keys.usage_since(offset, run_id or run_dir.name, task.stage)
        return SandboxResult(status=data.get("status", "failed"), out_dir=out, data=data, usage=usage)

    def run_tool(self, workspace: Path, args: list[str], writable: bool = False, timeout_s: int = 300,
                 network: str = "none", mounts: dict[Path, str] | None = None,
                 env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        """No LLM, no key. network="none" by default; the dependency-cache warmer passes the egress network."""
        mode = "" if writable else ":ro"
        cmd = ["docker", "run", "--rm", "--network", network, *HARDENING, *self.limits, *user_args(),
               "-v", f"{workspace}:/workspace{mode}", "-w", "/workspace",
               *[arg for host, inside in (mounts or {}).items() for arg in ("-v", f"{host}:{inside}")],
               *[arg for k, v in (env or {}).items() for arg in ("-e", f"{k}={v}")],
               "--entrypoint", args[0], self.image, *args[1:]]
        return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s)
