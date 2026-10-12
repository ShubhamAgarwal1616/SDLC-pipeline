"""Real Pipeline B stages (P5): B0 fetch, B1 prep, B3 implement, B4 verify.

B3 is the only LLM stage here: your `tdd` skill in the agent sandbox. B4 runs the build in a fresh
**verify container** (no network, no key, no LLM) on a clean clone of what the agent committed, so the
agent can't influence the official result. Phase 1 only clones, copies, starts containers and reads results.
"""

from __future__ import annotations

import fnmatch
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path

from sdlc import dep_cache
from sdlc.stacks import STACKS, Stack, detect as detect_stack
from sdlc.connectors import github, jira
from sdlc.engine.stage import StageContext, StageResult
from sdlc.openspec_parse import scenarios
from sdlc.sandbox import SandboxError
from sdlc.stages.pipeline_a import _agent_task, _run_agent, _run_dir, _runner

STOPWORDS = {"with", "from", "that", "this", "when", "then", "into", "each", "must", "shall", "have", "per", "and",
             "the", "for", "are", "not", "its", "their", "over", "under", "only", "change", "changes"}
TEXT_SUFFIXES = {".kt", ".kts", ".java", ".md", ".yaml", ".yml", ".json", ".properties", ".py", ".ts", ".js", ".sql"}


def _ticket(ctx: StageContext) -> dict:
    return json.loads(ctx.latest("b0_fetch", "ticket.json").read_text())


def _prep(ctx: StageContext) -> dict:
    return json.loads(ctx.latest("b1_prep", "prep.json").read_text())


def current_base_sha(ctx: StageContext) -> str:
    """The base commit the branch is measured against: B1's, or the newest one merged in by a sync."""
    for entry in reversed(ctx.run.history):
        if "sync.json" in entry.artifacts:
            return json.loads(Path(entry.artifacts["sync.json"]).read_text())["base_sha"]
    return _prep(ctx)["base_sha"]


def sync_bundle(ctx: StageContext) -> tuple[Path | None, str]:
    """If the base branch moved, or a person pushed to the PR branch, bundle those commits for the agent.

    Phase 1 builds the bundle in its own clone; the agent merges it inside the sandbox (offline) and resolves
    conflicts test-first. Returns (bundle, prompt text) or (None, "").
    """
    prep = _prep(ctx)
    repo, base, branch = prep["repo"], prep["base"], prep["branch"]
    base_now = github.request("GET", f"/repos/{repo}/branches/{base}")["commit"]["sha"]
    pr_file = ctx.latest("b7_pr", "pr.json")
    pushed = json.loads(pr_file.read_text()).get("sha") if pr_file else None
    try:
        remote_branch = github.request("GET", f"/repos/{repo}/branches/{branch}")["commit"]["sha"]
    except github.GitHubError:
        remote_branch = None
    base_moved = base_now != current_base_sha(ctx)
    human_commits = bool(pushed and remote_branch and remote_branch != pushed)
    if not (base_moved or human_commits):
        return None, ""
    sync = _run_dir(ctx) / "sync"
    if (sync / ".git").exists():
        github.git(sync, "fetch", "--quiet", "origin")
    else:
        github.clone(repo, sync, depth=None)
    refs = [f"refs/remotes/origin/{base}"] + ([f"refs/remotes/origin/{branch}"] if human_commits else [])
    bundle = ctx.out_dir / "sync.bundle"
    github.git(sync, "bundle", "create", str(bundle), *refs)
    ctx.write("sync.json", {"base_sha": base_now, "base_moved": base_moved, "human_commits": human_commits})
    steps = [f"`git fetch /io/in/inputs/sync.bundle '+refs/remotes/origin/*:refs/remotes/upstream/*'`"]
    if human_commits:
        steps.append(f"`git merge --no-edit upstream/{branch}` (a person pushed commits to the PR branch: keep them)")
    if base_moved:
        steps.append(f"`git merge --no-edit upstream/{base}` (the base branch moved)")
    text = ("FIRST, sync the branch: " + ", then ".join(steps) + ". Resolve any conflicts so all tests pass "
            "(test-first where behaviour is affected), and commit the merge.\n\n")
    return bundle, text


# ---- B0 fetch (script) --------------------------------------------------------------------------

class Fetch:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        key = ctx.run.inputs["ticket"]
        issue = jira.get_issue(key)
        f = issue["fields"]
        labels = f.get("labels") or []
        change_id = ctx.run.inputs.get("change_id") or next(
            (lbl[len("openspec-"):] for lbl in labels if lbl.startswith("openspec-")), None)
        if not change_id:
            raise SandboxError(f"{key} has no openspec-<change-id> label; pass --change-id or publish it via sdlc plan")
        repo_label = next((lbl[len("repo-"):] for lbl in labels if lbl.startswith("repo-")), None)
        from sdlc import registry
        repo = (ctx.run.inputs.get("repo") or (registry.load().get(repo_label, {}).get("github") if repo_label else None)
                or ctx.config.get("project.github.repo"))
        if not repo:
            raise SandboxError("no repo: pass --repo owner/name or set project.github.repo")
        ticket = {"key": key, "summary": f["summary"], "change_id": change_id, "repo": repo,
                  "status": f["status"]["name"], "type": f["issuetype"]["name"],
                  "epic": (f.get("parent") or {}).get("key"), "url": jira.browse_url(key)}
        moved = False
        if not ctx.run.inputs.get("dry_run") and (target := ctx.config.get("publish.transitions.start", "In Progress")):
            moved = f["status"]["name"].lower() != target.lower() and jira.transition(key, target)
        return StageResult("ok", {"ticket.json": ctx.write("ticket.json", ticket)},
                           note=f"{key} → {target}" if moved else None)


# ---- B1 prep (script) ---------------------------------------------------------------------------

def skill_hints(rules: list[dict], files: list[str], text: str) -> list[str]:
    found: list[str] = []
    lower = text.lower()
    for rule in rules or []:
        by_path = any(fnmatch.fnmatch(f, p) for p in rule.get("paths", []) for f in files)
        by_term = any(t.lower() in lower for t in rule.get("terms", []))
        if (by_path or by_term) and rule["skill"] not in found:
            found.append(rule["skill"])
    return found


def rank_files(ws: Path, files: list[str], terms: set[str], limit: int = 12) -> list[tuple[str, int]]:
    scored = []
    for rel in files:
        path = ws / rel
        if path.suffix not in TEXT_SUFFIXES or rel.startswith("openspec/") or not path.is_file():
            continue
        if path.stat().st_size > 200_000:
            continue
        text = path.read_text(errors="ignore").lower()
        score = sum(3 for t in terms if t in rel.lower()) + sum(min(text.count(t), 5) for t in terms)
        if score:
            scored.append((rel, score))
    return sorted(scored, key=lambda x: -x[1])[:limit]


def prep_stack(prep: dict) -> Stack | None:
    """The run's build stack (runs prepared before stacks existed recorded only a Gradle cache)."""
    name = prep.get("stack") or ("gradle" if prep.get("gradle_warm") else None)
    return STACKS.get(name) if name else None


def warm_cache(ctx: StageContext, prep: dict) -> Path | None:
    """The base branch's warmed dependency cache. A later job (CI) may not have it any more: warm it again."""
    stack, path = prep_stack(prep), prep.get("deps_warm") or prep.get("gradle_warm")
    if not stack or not path:
        return None
    if (Path(path) / ".complete").exists():
        return Path(path)
    runner = _runner(ctx)
    runner.preflight(need_gateway=False)
    return dep_cache.warm(runner, prep["repo"], prep["base"], stack, ctx.config.get("verify.build_files"))


def agent_cache(ctx: StageContext, prep: dict) -> tuple[Path, Stack] | None:
    """The run's private dependency cache for the agent, seeded on first use (or again in a new CI job)."""
    stack = prep_stack(prep)
    if not stack:
        return None
    home = _run_dir(ctx) / "deps" / stack.name if prep.get("stack") else _run_dir(ctx) / "gradle" / "agent"
    if not home.exists() and (warm := warm_cache(ctx, prep)):
        dep_cache.seed(warm, home)
    return (home, stack) if home.exists() else None


def mount_cache(ctx: StageContext, prep: dict, task) -> None:
    """Mount the dependency cache into an agent task, with the env that makes the build use only it."""
    if found := agent_cache(ctx, prep):
        home, stack = found
        task.mounts[home] = stack.mount
        task.env.update(stack.env)


class Prep:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        ticket = _ticket(ctx)
        repo, cid, key = ticket["repo"], ticket["change_id"], ticket["key"]
        base = ctx.config.get("project.github.base_branch") or github.repo_info(repo)["default_branch"]
        run_dir = _run_dir(ctx)
        ws = run_dir / "workspace"
        branch = f"feature/{key}-{cid}"[:100]
        if not (ws / ".git").exists():   # a fresh clone; the agent hasn't touched it yet, so git here is safe
            github.clone(repo, ws, branch=base, depth=50)
            github.git(ws, "checkout", "--quiet", "-b", branch)
        change_dir = ws / "openspec" / "changes" / cid
        if not change_dir.is_dir():
            raise SandboxError(f"openspec/changes/{cid} is not on {base}; was the spec PR merged?")
        base_sha = github.git(ws, "rev-parse", "HEAD").stdout.strip()
        files = github.git(ws, "ls-files").stdout.splitlines()

        warm = None
        stack = detect_stack(ws, ctx.config.get("project.stack"))
        if stack:
            runner = _runner(ctx)
            runner.preflight(need_gateway=False)
            warm = dep_cache.warm(runner, repo, base, stack, ctx.config.get("verify.build_files"))
            if not (run_dir / "deps" / stack.name).exists():
                dep_cache.seed(warm, run_dir / "deps" / stack.name)

        contracts = provider_contracts(ctx, repo)
        proposal = (change_dir / "proposal.md").read_text() if (change_dir / "proposal.md").exists() else ""
        scen = scenarios(change_dir)
        words = re.findall(r"[a-zA-Z][a-zA-Z-]{3,}", " ".join([cid, ticket["summary"], proposal] +
                                                             [s["name"] for s in scen]).lower())
        terms = {w for w in words if w not in STOPWORDS}
        ranked = rank_files(ws, files, terms)
        hints = skill_hints(ctx.config.get("skills.hints", []), files, f"{ticket['summary']} {proposal}")
        tasks_md = (change_dir / "tasks.md").read_text() if (change_dir / "tasks.md").exists() else "(none)"
        pack = "\n".join([
            f"# Context pack: {key} — {ticket['summary']}",
            f"Branch: `{branch}` (from `{base}` @ {base_sha[:10]})",
            f"Spec: `openspec/changes/{cid}/` (proposal.md, design.md, specs/, tasks.md)",
            "", "## Verify commands (run offline)", *[f"- `{c}`" for c in ctx.config.get("verify.commands", [])],
            "", "## Relevant files (ranked by overlap with the spec)",
            *[f"{i}. `{rel}` (score {score})" for i, (rel, score) in enumerate(ranked, 1)],
            "", "## Skill hints", *([f"- {h}" for h in hints] or ["- (none)"]),
            *(["", "## Provider contracts (read-only; their merged living specs)",
               *[f"- `/opt/sdlc/contracts/{name}/` ({gh})" for name, gh in contracts.items()],
               "Code against these. Never change provider behaviour from this repo."] if contracts else []),
            "", "## Scenarios to cover", *[f"- {s['name']}: " + "; ".join(s["steps"]) for s in scen],
            "", "## Tasks (the plan)", tasks_md,
            "", f"## All tracked files ({len(files)})", *[f"- {f}" for f in files[:150]],
        ])
        prep = {"repo": repo, "base": base, "base_sha": base_sha, "branch": branch, "change_id": cid,
                "stack": stack.name if stack else None, "deps_warm": str(warm) if warm else None,
                "skill_hints": hints, "contracts": contracts}
        return StageResult("ok", {"context-pack.md": ctx.write("context-pack.md", pack),
                                  "prep.json": ctx.write("prep.json", prep)},
                           gate_info={"design": f"openspec/changes/{cid}/design.md"})


def provider_contracts(ctx: StageContext, repo: str) -> dict[str, str]:
    """For a consumer repo (config/repos.yaml depends_on), copy each provider's merged living specs
    (openspec/specs/**) into runs/<id>/contracts/<provider>/. Mounted read-only into the sandbox."""
    from sdlc import registry
    from sdlc.init_repo import project_snapshot
    from sdlc.stages.publish import safe_copy_tree
    repos = registry.load()
    me = registry.name_for(repo, repos)
    found: dict[str, str] = {}
    for dep in (repos.get(me, {}).get("depends_on", []) if me else []) or []:
        gh = repos[dep]["github"]
        snap = project_snapshot(gh)
        dest = _run_dir(ctx) / "contracts" / dep
        if (snap / "openspec" / "specs").is_dir():
            safe_copy_tree(snap / "openspec" / "specs", dest / "specs")
            found[dep] = gh
    return found


def mount_contracts(ctx: StageContext, task) -> None:
    contracts_dir = _run_dir(ctx) / "contracts"
    if contracts_dir.is_dir() and any(contracts_dir.iterdir()):
        task.mounts[contracts_dir] = "/opt/sdlc/contracts:ro"
        task.add_dirs.append("/opt/sdlc/contracts")


# ---- B3 implement (LLM: /tdd) ------------------------------------------------------------------------

class Implement:
    def __init__(self, name: str):
        self.name = name

    def _prompt(self, ctx: StageContext, ticket: dict, prep: dict) -> str:
        if ctx.stage not in ctx.run.sessions:
            hints = ", ".join(prep.get("skill_hints") or []) or "none"
            return (f"/tdd Implement {ticket['key']}: {ticket['summary']}.\n"
                    f"Spec: openspec/changes/{ticket['change_id']}/ (tasks.md is the plan; the WHEN/THEN scenarios "
                    f"in specs/ are the behaviours to test).\nContext pack: /io/in/inputs/context-pack.md.\n"
                    f"Relevant skills: {hints}.\nFollow the stage contract.")
        parts = []
        for f in ctx.feedback:
            src = f.get("source")
            if src == "b4_verify":
                parts.append(f"The clean verify run FAILED:\n{f.get('note')}")
            elif src == "b5_review":
                parts.append(f"The code review found blocking issues:\n{f.get('note')}")
            elif src == "b8_fix" or f.get("data"):
                parts.append(f"Review feedback:\n{f.get('note') or f.get('data', {}).get('text')}")
        body = "\n\n".join(parts) or ("Continue and finish per the stage contract. Re-read it first: the contract "
                                     "and the allowed commands may have been updated since your last turn.")
        return f"{body}\n\nFix it (test-first for behaviour problems), commit, and finish per the stage contract."

    def run(self, ctx: StageContext) -> StageResult:
        ticket, prep = _ticket(ctx), _prep(ctx)
        inputs = {"context-pack.md": ctx.latest("b1_prep", "context-pack.md")}
        bundle, sync_text = sync_bundle(ctx) if ctx.attempt > 1 else (None, "")
        if bundle:
            inputs["sync.bundle"] = bundle
        task = _agent_task(ctx, sync_text + self._prompt(ctx, ticket, prep), "implement.md", inputs,
                           ["impl-summary.json"], ctx.run.sessions.get(ctx.stage))
        mount_cache(ctx, prep, task)
        mount_contracts(ctx, task)
        result = _run_agent(ctx, task)
        summary_file = result.file("impl-summary.json")
        if not summary_file.exists():
            raise SandboxError(f"implement produced no impl-summary.json: {result.failure()}")
        summary = json.loads(summary_file.read_text())
        artifacts = {"impl-summary.json": ctx.write("impl-summary.json", summary)}
        if (ctx.out_dir / "sync.json").exists():
            artifacts["sync.json"] = str(ctx.out_dir / "sync.json")
        if summary.get("status") == "blocked":
            return StageResult("blocked", artifacts, note=f"agent blocked: {summary.get('notes')}", usage=result.usage)
        return StageResult("ok", artifacts, note=summary.get("tests"), usage=result.usage)


# ---- B4 verify (script; verify container: no network, no key, no LLM) ------------------------------

def junit_report(src: Path) -> dict:
    totals = {"tests": 0, "failures": 0, "errors": 0, "skipped": 0}
    failed = []
    for xml in (x for x in src.glob("**/test-results/**/*.xml") if "node_modules" not in x.parts):
        try:
            root = ET.parse(xml).getroot()
        except ET.ParseError:
            continue
        for case in root.iter("testcase"):   # counted per case: Gradle and Node (node --test) agree on these
            totals["tests"] += 1
            totals["failures"] += bool(case.findall("failure"))
            totals["errors"] += bool(case.findall("error"))
            totals["skipped"] += bool(case.findall("skipped"))
            for bad in list(case.findall("failure")) + list(case.findall("error")):
                failed.append(f"{case.get('classname')} > {case.get('name')}: "
                              f"{(bad.get('message') or '').strip()[:300]}")
    return {**totals, "failed": failed[:20]}


SEMGREP_IGNORE = "build/\n.gradle/\n.git/\n.kotlin/\nnode_modules/\ndist/\ncoverage/\n"


def run_semgrep(runner, src: Path) -> dict:
    """Org rules, offline. Our own .semgrepignore always replaces the repo's: the defaults would skip test
    dirs, and a committed .semgrepignore must not be able to hide code from the scan."""
    (src / ".semgrepignore").write_text(SEMGREP_IGNORE)
    proc = runner.run_tool(src, ["semgrep", "scan", "--config", "/opt/sdlc/semgrep-rules", "--metrics=off",
                                 "--disable-version-check", "--quiet", "--json"], writable=True, timeout_s=600)
    try:
        data = json.loads(proc.stdout)
    except ValueError:
        return {"errors": [f"semgrep did not run: {proc.stderr.strip()[-300:]}"], "warnings": []}
    fmt = lambda r: f"{r['path']}:{r['start']['line']} [{r['check_id'].split('.')[-1]}] {r['extra']['message']}"  # noqa: E731
    return {"errors": [fmt(r) for r in data.get("results", []) if r["extra"].get("severity") == "ERROR"],
            "warnings": [fmt(r) for r in data.get("results", []) if r["extra"].get("severity") != "ERROR"]}


def failure_digest(log: str, report: dict, max_lines: int = 60) -> str:
    compile_errors = [line for line in log.splitlines()
                      if any(re.match(s.compile_error, line) for s in STACKS.values())][:20]
    parts = []
    if compile_errors:
        parts.append("Compilation errors:\n" + "\n".join(compile_errors))
    if report.get("failed"):
        parts.append("Failing tests:\n" + "\n".join(f"- {f}" for f in report["failed"]))
    tail = "\n".join(log.splitlines()[-max_lines:])
    parts.append(f"Build output (tail):\n{tail}")
    return "\n\n".join(parts)


class Verify:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        runner = _runner(ctx)
        runner.preflight(need_gateway=False)
        ticket, prep = _ticket(ctx), _prep(ctx)
        run_dir = _run_dir(ctx)
        work = run_dir / "verify" / f"attempt-{ctx.attempt}"
        src = work / "src"
        # a clean clone of what the agent COMMITTED (uncommitted edits don't count). Phase 1 never runs
        # git inside the agent's workspace; a local clone copies objects only, not its hooks or config.
        github.git(None, "clone", "--quiet", "--no-hardlinks", str(run_dir / "workspace"), str(src))
        base_sha = current_base_sha(ctx)
        commits = github.git(src, "rev-list", "--count", f"{base_sha}..HEAD").stdout.strip()
        changed = github.git(src, "diff", "--name-only", base_sha, "HEAD").stdout.split()
        checks: dict[str, str] = {"has_commits": "pass" if int(commits or 0) > 0 else "fail"}
        logs: dict[str, str] = {}

        leaks = runner.run_tool(src, ["gitleaks", "dir", "/workspace", "--no-banner", "--redact", "--exit-code", "2"])
        checks["gitleaks"] = "pass" if leaks.returncode == 0 else "fail"
        logs["gitleaks"] = (leaks.stdout + leaks.stderr)[-3000:]

        semgrep = run_semgrep(runner, src)
        checks["semgrep"] = "fail" if semgrep["errors"] else "pass"
        logs["semgrep"] = json.dumps(semgrep, indent=2)

        mounts, env = {}, {}
        if (warm := warm_cache(ctx, prep)) and (stack := prep_stack(prep)):
            home = dep_cache.seed(warm, work / "deps")
            mounts, env = {home: stack.mount}, dict(stack.env)
        build_log = []
        build_ok = True
        for cmd in ctx.config.get("verify.commands", []):
            proc = runner.run_tool(src, ["sh", "-c", f"{cmd} 2>&1"], writable=True, timeout_s=1800,
                                   mounts=mounts, env=env)
            build_log.append(f"$ {cmd}\n{proc.stdout}{proc.stderr}")
            if proc.returncode != 0:
                build_ok = False
                break
        logs["build"] = "\n".join(build_log)
        checks["build_and_tests"] = "pass" if build_ok else "fail"
        report = junit_report(src)

        listing = runner.run_tool(src, ["openspec", "list", "--json"])
        try:
            entry = next(c for c in json.loads(listing.stdout).get("changes", []) if c["name"] == ticket["change_id"])
            done, total = entry.get("completedTasks", 0), entry.get("totalTasks", 0)
        except (ValueError, StopIteration):
            done, total = 0, -1
        checks["tasks_complete"] = "pass" if total > 0 and done == total else "fail"

        for name, text in logs.items():
            (work / f"{name}.log").write_text(text)
        verify = {"passed": all(v == "pass" for v in checks.values()), "checks": checks, "tests": report,
                  "semgrep": {"errors": len(semgrep["errors"]), "warnings": semgrep["warnings"][:20]},
                  "commits": int(commits or 0), "files_changed": changed, "tasks": f"{done}/{total}"}
        artifacts = {"verify.json": ctx.write("verify.json", verify)}
        if verify["passed"]:
            return StageResult("pass", artifacts, note=f"{report['tests']} tests passed, {len(changed)} files changed")
        problems = []
        if checks["has_commits"] == "fail":
            problems.append("No commits on the branch: commit your work.")
        if checks["tasks_complete"] == "fail":
            problems.append(f"tasks.md shows {done}/{total} tasks done: tick completed tasks (- [x]) and commit.")
        if checks["gitleaks"] == "fail":
            problems.append("gitleaks found a possible secret in the code:\n" + logs["gitleaks"][-1500:])
        if checks["semgrep"] == "fail":
            problems.append("semgrep (org rules) found errors:\n" + "\n".join(f"- {f}" for f in semgrep["errors"][:15]))
        if checks["build_and_tests"] == "fail":
            problems.append(failure_digest(logs["build"], report))
        return StageResult("fail", artifacts, note="\n\n".join(problems))
