"""`sdlc doctor`: is this machine and this repo ready for the pipeline? (PLAN.md §17)

Every check is read-only. Secrets are only checked for presence, never printed.
"""

from __future__ import annotations

import json
import subprocess
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from sdlc import secrets
from sdlc.config import ROOT, ConfigError, EffectiveConfig, load_config
from sdlc.skills_lint import lint_dir

CLAUDE_MD_WARN_CHARS = 8000   # ~2k tokens; always loaded into context


@dataclass
class Check:
    name: str
    status: str            # pass | warn | fail
    detail: str = ""


def _run(cmd: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, capture_output=True, text=True)


def local_checks(cfg: EffectiveConfig) -> list[Check]:
    out: list[Check] = []
    docker = _run(["docker", "info", "--format", "{{.ServerVersion}}"])
    out.append(Check("docker engine", "pass" if docker.returncode == 0 else "fail",
                     docker.stdout.strip() or docker.stderr.strip()[:120]))
    from sdlc.infra import images
    for image in images(cfg).values():
        ok = _run(["docker", "image", "inspect", image]).returncode == 0
        out.append(Check(f"image {image}", "pass" if ok else "warn",
                         "" if ok else "not pulled yet: `sdlc up` (or the first run) pulls it"))
    for net in (cfg.get("sandbox.docker_network", "sdlc_internal"), "sdlc_egress"):
        info = _run(["docker", "network", "inspect", net, "--format", "{{.Internal}}"])
        internal_ok = net != cfg.get("sandbox.docker_network", "sdlc_internal") or info.stdout.strip() == "true"
        out.append(Check(f"network {net}", "pass" if info.returncode == 0 and internal_ok else "fail",
                         f"internal={info.stdout.strip()}" if info.returncode == 0 else "run `sdlc up`"))
    try:
        with urllib.request.urlopen(cfg.get("sandbox.gateway_health_url"), timeout=5) as resp:
            health = json.loads(resp.read())
        out.append(Check("LLM gateway", "pass", f"{health.get('upstream')} ({health.get('priced_models')} priced models)"))
    except Exception as exc:
        out.append(Check("LLM gateway", "warn", f"not running ({exc}): `sdlc up`, or the first run starts it"))
    stages = cfg.get("llm.stages", {}) or {}
    models = ", ".join(f"{s}={v.get('model')}" for s, v in stages.items())
    same = stages.get("review", {}).get("model") == stages.get("implement", {}).get("model")
    out.append(Check("stage models", "warn" if same else "pass",
                     models + ("  (review uses the implement model; a different one catches more)" if same else "")))
    provider_key = {"openrouter": "OPENROUTER_API_KEY", "anthropic": "ANTHROPIC_API_KEY"}.get(cfg.get("llm.provider"))
    needed = [provider_key] if provider_key else []
    if cfg.get("project.scm") == "github":
        needed.append("GITHUB_TOKEN")
    if cfg.get("project.tracker") == "jira":
        needed += ["JIRA_BASE_URL", "JIRA_USER_EMAIL", "JIRA_API_TOKEN"]
    missing = [n for n in needed if not secrets.get(n, required=False)]
    out.append(Check(".env secrets present", "fail" if missing else "pass",
                     f"missing: {', '.join(missing)}" if missing else ", ".join(needed)))
    lint = lint_dir(ROOT / "skills", repo_level=False)
    errors = [f for f in lint if f.level == "error"]
    out.append(Check("org skills lint", "fail" if errors else ("warn" if lint else "pass"),
                     f"{len(errors)} errors, {len(lint) - len(errors)} warnings" if lint else "clean"))
    return out


def repo_checks(repo: str, cfg: EffectiveConfig, snapshot: Path) -> list[Check]:
    from sdlc.connectors import github, jira
    out: list[Check] = []
    try:
        info = github.repo_info(repo)
        perms = info.get("permissions") or {}
        out.append(Check("GitHub repo access", "pass" if perms.get("push") else "warn",
                         f"default branch {info['default_branch']}; push={perms.get('push')}"))
        branch = github.request("GET", f"/repos/{repo}/branches/{info['default_branch']}")
        out.append(Check("base branch protected", "pass" if branch.get("protected") else "warn",
                         "" if branch.get("protected") else "protect it so only reviewed PRs reach it"))
    except Exception as exc:
        out.append(Check("GitHub repo access", "fail", str(exc)[:160]))
    out.append(Check(".sdlc/pipeline.yaml", "pass" if (snapshot / ".sdlc" / "pipeline.yaml").exists() else "fail",
                     "" if (snapshot / ".sdlc" / "pipeline.yaml").exists() else "run `sdlc init --repo …`"))
    out.append(Check("openspec/", "pass" if (snapshot / "openspec").is_dir() else "fail"))
    claude_md = snapshot / "CLAUDE.md"
    if claude_md.exists():
        size = len(claude_md.read_text())
        out.append(Check("CLAUDE.md size", "warn" if size > CLAUDE_MD_WARN_CHARS else "pass",
                         f"{size} chars (~{size // 4} tokens; always in context)"))
    else:
        out.append(Check("CLAUDE.md", "warn", "missing: conventions help the agent"))
    from sdlc.stacks import detect
    try:
        stack = detect(snapshot, cfg.get("project.stack"))
    except ValueError as exc:
        stack = None
        out.append(Check("build stack", "fail", str(exc)))
    if stack:
        out.append(Check(f"verify commands ({stack.name})", "pass" if cfg.get("verify.commands") else "fail",
                         "; ".join(cfg.get("verify.commands") or ["(none configured)"])))
    elif (snapshot / "package.json").exists() and not (snapshot / "package-lock.json").exists():
        out.append(Check("build stack", "warn", "package.json without package-lock.json: commit the lockfile so "
                                                "dependencies can be cached for offline builds"))
    repo_skills = snapshot / ".claude" / "skills"
    if repo_skills.is_dir():
        lint = lint_dir(repo_skills, repo_level=True)
        errors = [f for f in lint if f.level == "error"]
        out.append(Check("repo skills lint", "fail" if errors else ("warn" if lint else "pass"),
                         f"{len(errors)} errors, {len(lint) - len(errors)} warnings" if lint else "clean"))
    if cfg.get("project.tracker") == "jira" and (key := cfg.get("project.jira.project_key")):
        try:
            proj = jira.request("GET", f"/rest/api/3/project/{key}")
            types = {t["name"] for t in proj.get("issueTypes", [])}
            want = {cfg.get("publish.story_type", "Story"), cfg.get("publish.epic_type", "Epic")}
            out.append(Check(f"Jira project {key}", "pass" if want <= types else "fail",
                             f"issue types {sorted(types)}"))
        except Exception as exc:
            out.append(Check(f"Jira project {key}", "fail", str(exc)[:160]))
    if cfg.get("project.docs") == "confluence":
        from sdlc.connectors import confluence
        try:
            conf = confluence.client(cfg)
            fake = isinstance(conf, confluence.FakeConfluence)
            out.append(Check(f"Confluence space {cfg.get('confluence.space')}", "pass" if conf.space_ok() else "fail",
                             "fake: pages go to runs/_confluence/" if fake else ""))
        except Exception as exc:
            out.append(Check("Confluence", "fail", str(exc)[:160]))
    return out


def run_doctor(repo: str | None) -> list[Check]:
    snapshot = None
    if repo:
        from sdlc.init_repo import project_snapshot
        snapshot = project_snapshot(repo)
    try:
        cfg = load_config(snapshot)
        checks = [Check("config + org locks", "pass")]
    except ConfigError as exc:
        return [Check("config + org locks", "fail", str(exc))]
    checks += local_checks(cfg)
    if repo and snapshot:
        checks += repo_checks(repo, cfg, snapshot)
    return checks
