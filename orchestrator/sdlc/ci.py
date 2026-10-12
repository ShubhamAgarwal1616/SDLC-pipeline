"""CI mode: the pipeline running inside a repo's GitHub Actions (`sdlc ci reconcile`).

Every workflow trigger runs one idempotent reconcile over the whole repo, so an event GitHub's concurrency
queue drops is handled by the next run:
  1. restore the run state the previous job saved (an encrypted artifact),
  2. orphaned runs (the previous job died mid-stage) escalate, so a human can `/sdlc resume`,
  3. new issues labelled `sdlc` from collaborators start Pipeline A (the issue is the requirement),
  4. an edited issue updates its run's requirement; `/sdlc …` commands in those issues answer the grill,
     start implementation, resume or abort,
  5. `watch.poll_once` handles the PR gates (revise, fix, approve, merge, close), reminders and features,
  6. the issue gets a comment at each milestone (questions, spec PR, stories, code PRs, escalations),
  7. the state is encrypted (SDLC_STATE_KEY) and written for the workflow to upload.
Commands follow the same trust rules as PR commands: repo owners, members and collaborators only.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import re
import tarfile
import zipfile
from pathlib import Path

from sdlc import answers, secrets
from sdlc.config import HOME, RUNS, load_config, version
from sdlc.connectors import github
from sdlc.engine import Engine, EngineError, RunState, RunStatus, RunStore
from sdlc.engine.state import now

STATE_ARTIFACT = "sdlc-state"
LABEL = "sdlc"
CI_FILE = RUNS / "_ci" / "ci.json"     # not state.json: the run store treats those as runs
OUT = HOME / "out" / "state.enc"
SKIP_TOP = {"_cache", "_evals", "_dashboard", "_init", "_ci_tmp"}
SKIP_IN_RUN = {"gradle", "deps", "verify", "candidate", "sync", "publish", "archive"}   # rebuilt on demand
SKIP_IN_WORKSPACE = {"build", ".gradle", ".kotlin", "node_modules", "dist", "coverage"}

HELP = """**SDLC pipeline commands** (in this issue):
- `/sdlc answer <text>`: answer the open question (several questions: one `r1-q2: <answer>` per line)
- `/sdlc accept`: take the recommended answer(s) · `/sdlc done`: stop asking; open points become assumptions
- `/sdlc implement KAN-12 [KAN-13 …]` or `/sdlc implement all`: build stories once the spec is merged
- `/sdlc resume [stage] [key=value …]`: continue an escalated run (e.g. `budgets.per_stage_usd.review=6`)
- `/sdlc abort` · `/sdlc status` · `/sdlc help`

On the PRs: `/sdlc revise` (spec PR), `/sdlc fix`, `/sdlc approve`, `/sdlc abort`. Merging a PR approves it."""


# ---- run state between jobs ---------------------------------------------------------------------------

def _box():
    from nacl import hash as nacl_hash, encoding, secret
    raw = secrets.get("SDLC_STATE_KEY")
    return secret.SecretBox(nacl_hash.blake2b(raw.encode(), digest_size=32, encoder=encoding.RawEncoder))


def _keep(rel: Path) -> bool:
    parts = rel.parts
    if not parts or parts[0] in SKIP_TOP:
        return False
    if len(parts) >= 2 and parts[0].startswith("run-"):
        if parts[1] in SKIP_IN_RUN:
            return False
        if parts[1] == "claude-home" and len(parts) >= 3 and parts[2] == "skills":   # re-synced from the image
            return False
        if parts[1] == "workspace" and len(parts) >= 3 and parts[2] in SKIP_IN_WORKSPACE:
            return False
    return True


def _safe_member(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
    """tarfile's 'data' filter, but an unsafe member (e.g. a symlink an agent left pointing outside the
    workspace) is skipped instead of failing the whole restore."""
    try:
        return tarfile.data_filter(member, path)
    except tarfile.FilterError:
        return None


def pack() -> bytes:
    """Encrypted tar.gz of the run state (everything that can't be rebuilt)."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = json.dumps({"home": str(HOME), "version": version(), "at": now()}).encode()
        info = tarfile.TarInfo("_meta.json")
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
        if RUNS.exists():
            tar.add(RUNS, arcname="runs", filter=lambda ti: ti if _keep(Path(ti.name).relative_to("runs"))
                    or ti.name == "runs" else None)
    return _box().encrypt(buf.getvalue())


def unpack(blob: bytes) -> dict:
    data = _box().decrypt(blob)
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
        meta = json.loads(tar.extractfile("_meta.json").read())
        members = [m for m in tar.getmembers() if m.name.startswith("runs/") or m.name == "runs"]
        tar.extractall(RUNS.parent, members=members, filter=_safe_member)
    old_home = meta.get("home")
    if old_home and old_home != str(HOME):   # artifact paths in state are absolute: rebase them
        for path in [*RUNS.glob("run-*/state.json"), *RUNS.glob("_features/*.json")]:
            path.write_text(path.read_text().replace(old_home, str(HOME)))
    return meta


def restore(repo: str) -> str:
    token = os.environ.get("SDLC_ACTIONS_TOKEN") or None
    artifact = github.latest_artifact(repo, STATE_ARTIFACT, token)
    if not artifact:
        return "no saved state (first run)"
    raw = github.download_artifact(repo, artifact["id"], token)
    with zipfile.ZipFile(io.BytesIO(raw)) as z:
        blob = z.read("state.enc")
    meta = unpack(blob)
    return f"restored state from {meta.get('at')} (artifact {artifact['id']})"


def save() -> Path | None:
    if not RUNS.exists() or not any(RUNS.glob("run-*/state.json")):
        return None
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_bytes(pack())
    return OUT


# ---- bookkeeping ----------------------------------------------------------------------------------------

def _load_ci() -> dict:
    return json.loads(CI_FILE.read_text()) if CI_FILE.exists() else {"seen": [], "notices": {}}


def _save_ci(state: dict) -> None:
    state["seen"] = state["seen"][-3000:]
    CI_FILE.parent.mkdir(parents=True, exist_ok=True)
    CI_FILE.write_text(json.dumps(state, indent=2))


def _trusted(item: dict) -> bool:
    return (item.get("author_association") or item.get("association")) in github.TRUSTED_ASSOCIATIONS


def runs_for_issue(store: RunStore, number: int, pipeline: str | None = None) -> list[RunState]:
    return [r for r in store.list() if r.inputs.get("issue") == number and (not pipeline or r.pipeline == pipeline)]


REPOS_SECTION = re.compile(r"^###\s+Repos\s*\n(.*?)(?=^###\s|\Z)", re.M | re.S)


def requirement_text(issue: dict) -> str:
    title = re.sub(r"^\[feature\]\s*", "", issue["title"].strip(), flags=re.I)
    body = REPOS_SECTION.sub("", issue.get("body") or "")
    body = "\n".join(line for line in body.splitlines() if line.strip() != "_No response_")
    return f"# {title}\n\n{body.strip()}\n"


def issue_repos(issue: dict) -> list[str]:
    """Registry names from the form's optional "Repos" field (a multi-repo feature)."""
    m = REPOS_SECTION.search(issue.get("body") or "")
    text = m.group(1) if m else ""
    return [n for n in re.split(r"[\s,]+", text.replace("_No response_", "")) if n]


def ensure_registry(repo: str) -> dict:
    """In CI the registry is the home repo's .sdlc/repos.yaml (copied to where `registry` reads it)."""
    from sdlc import registry
    from sdlc.init_repo import project_snapshot
    src = project_snapshot(repo) / ".sdlc" / "repos.yaml"
    if src.is_file():
        registry.REGISTRY.parent.mkdir(parents=True, exist_ok=True)
        registry.REGISTRY.write_text(src.read_text())
    return registry.load()


def cross_repo_token() -> bool:
    """The workflow's own token can only reach its repo; a feature across repos needs SDLC_GITHUB_TOKEN."""
    actions = os.environ.get("SDLC_ACTIONS_TOKEN")
    return not actions or os.environ.get("GITHUB_TOKEN") != actions


def _unbuilt(pipeline: str) -> list[str]:
    from sdlc.engine.runner import stages_of
    from sdlc.stages import REAL_STAGES
    return [s for s in stages_of(pipeline) if s not in REAL_STAGES]


def orphaned(engine: Engine) -> list[str]:
    """Jobs run one at a time, so a run still `running` when a job starts was cut off (timeout or cancel)."""
    out = []
    for run in engine.store.list():
        if run.status == RunStatus.RUNNING:
            run.status = RunStatus.ESCALATED
            run.escalation = {"at": run.current, "outcome": "interrupted", "since": now(),
                              "reason": f"the CI job stopped while {run.current} was running (timeout or cancel)"}
            engine.store.log_event(run.run_id, "escalated", at=run.current, reason="ci job interrupted")
            engine.store.save(run)
            out.append(f"{run.run_id}: interrupted at {run.current} → escalated")
    return out


# ---- new requirements ---------------------------------------------------------------------------------

def start_new(engine: Engine, repo: str, issues: list[dict], ci: dict) -> list[str]:
    from sdlc.init_repo import project_snapshot
    actions = []
    for issue in issues:
        n = issue["number"]
        if runs_for_issue(engine.store, n, "plan"):
            continue
        if not _trusted(issue):
            if f"untrusted-{n}" not in ci["seen"]:
                ci["seen"].append(f"untrusted-{n}")
                github.comment(repo, n, "Only repo collaborators can start the SDLC pipeline. A collaborator can "
                                        "open a new issue with this content.")
            continue
        req = RUNS / "_ci" / "requirements" / f"issue-{n}.md"
        req.parent.mkdir(parents=True, exist_ok=True)
        req.write_text(requirement_text(issue))
        names, project_repo = issue_repos(issue) or None, repo
        if names:
            from sdlc import registry
            reg = ensure_registry(repo)
            unknown = [x for x in names if x not in reg]
            problem = (f"unknown repos {unknown}; registered in .sdlc/repos.yaml: {sorted(reg)}" if unknown else
                       None if cross_repo_token() else
                       "a feature across repos needs the secret SDLC_GITHUB_TOKEN: a token that can reach every repo "
                       "in it (the workflow's own token only reaches this repo)")
            if problem:
                marker = f"bad-repos-{n}-{hashlib.sha1(problem.encode()).hexdigest()[:8]}"   # once per problem
                if marker not in ci["seen"]:
                    ci["seen"].append(marker)
                    github.comment(repo, n, f"Can't start this multi-repo feature: {problem}. Edit the issue to fix it.")
                continue
            project_repo = reg[registry.ordered(names, reg)[0]]["github"]   # config from the first provider
        cfg = load_config(project_snapshot(project_repo))
        inputs = {"requirements": str(req), "context": None, "project": str(HOME), "repo": project_repo,
                  "repos": names, "dry_run": False, "issue": n}
        github.comment(repo, n, "Starting the SDLC pipeline on this requirement. Questions will appear here.")
        state = engine.start("plan", cfg, inputs, fake_stages=_unbuilt("plan"))
        actions.append(f"issue #{n}: started {state.run_id} [{state.status.value}]")
    return actions


def apply_edits(engine: Engine, repo: str, issues: list[dict], ci: dict) -> list[str]:
    """An edited issue updates its plan run's requirement (like `sdlc plan --run RUN --update`)."""
    actions = []
    for issue in issues:
        plan = _waiting_plan(engine, issue["number"])
        if not plan or plan.status not in (RunStatus.WAITING_HUMAN, RunStatus.ESCALATED):
            continue
        text = requirement_text(issue)
        if Path(plan.inputs["requirements"]).read_text() == text:
            continue
        new = RUNS / "_ci" / "requirements" / f"issue-{issue['number']}-edit.md"
        new.write_text(text)
        github.comment(repo, issue["number"], "The requirement changed: re-checking it. Only questions about what "
                                              "changed will be asked, and the spec will be revised.")
        state = engine.update_requirements(plan.run_id, new)
        actions.append(f"issue #{issue['number']}: requirement updated → {state.status.value}")
    return actions


# ---- commands in issues ---------------------------------------------------------------------------------

def parse_answers(text: str, questions: list[dict]) -> list[dict] | None:
    """`r1-q2: answer` lines, or the whole text when only one question is open."""
    ids = {q["id"] for q in questions}
    picked = {}
    for line in text.splitlines():
        m = re.match(r"^\s*(r\d+-q?\w+)\s*[:\-–]\s*(.+)$", line)
        if m and m.group(1) in ids:
            picked[m.group(1)] = m.group(2).strip()
    if picked:
        return answers.from_values(questions, picked)
    if len(questions) == 1 and text.strip():
        return answers.from_values(questions, {questions[0]["id"]: text.strip()})
    return None


def _waiting_plan(engine: Engine, n: int) -> RunState | None:
    runs = runs_for_issue(engine.store, n, "plan")
    return runs[0] if runs else None


def _latest_epic(run: RunState) -> str | None:
    for h in reversed(run.history):
        if h.stage == "a4_publish" and "published.json" in h.artifacts and Path(h.artifacts["published.json"]).exists():
            return json.loads(Path(h.artifacts["published.json"]).read_text()).get("epic")
    return None


def handle_command(engine: Engine, repo: str, n: int, comment: dict) -> str:
    body = comment["body"].strip()
    first, _, rest = body.partition("\n")
    words = first.split()
    cmd = words[1].lower() if len(words) > 1 else "help"
    args = words[2:]
    tail = (" ".join(args) + ("\n" + rest if rest else "")).strip()
    plan = _waiting_plan(engine, n)

    if cmd in {"answer", "accept", "done"}:
        if not plan or plan.status != RunStatus.WAITING_HUMAN or not plan.wait or plan.wait.gate != "h0_answers":
            return f"Nothing is waiting for answers here ({plan.status.value if plan else 'no run'})."
        questions = plan.wait.info.get("questions", [])
        if cmd == "done":
            state = engine.deliver(plan.run_id, "done", {"answers": answers.defaults(questions), "channel": "issue"},
                                   event_id=comment["id"])
        else:
            result = answers.defaults(questions) if cmd == "accept" else parse_answers(tail, questions)
            if result is None:
                return ("Several questions are open: answer each on its own line, e.g. `r1-q1: …`, or use "
                        "`/sdlc accept`.")
            state = engine.deliver(plan.run_id, "answered", {"answers": result, "channel": "issue",
                                                              "by": comment["author"]}, event_id=comment["id"])
        return f"{state.run_id}: {cmd} → {state.status.value}"

    if cmd == "implement":
        if not args:
            return "Say which stories: `/sdlc implement KAN-12` or `/sdlc implement all`."
        if args[0].lower() == "all":
            epic = _latest_epic(plan) if plan else None
            if not epic:
                return "No published Epic for this issue yet: merge the spec PR first."
            from sdlc import feature
            lines = feature.start(engine, epic, default_repo=repo, inputs={"issue": n})
            return "\n".join(lines) or f"feature {epic}: nothing to start yet"
        from sdlc.init_repo import project_snapshot
        cfg = load_config(project_snapshot(repo))
        out = []
        for key in args:
            if any(r.inputs.get("ticket") == key and r.status not in (RunStatus.DONE, RunStatus.ABORTED)
                   for r in engine.store.list()):
                out.append(f"{key}: already running")
                continue
            state = engine.start("implement", cfg, {"ticket": key, "project": str(HOME), "repo": repo,
                                                    "dry_run": False, "issue": n}, fake_stages=_unbuilt("implement"))
            out.append(f"{key}: {state.run_id} [{state.status.value}]")
        return "\n".join(out)

    if cmd == "resume":
        run = next((r for r in runs_for_issue(engine.store, n) if r.status == RunStatus.ESCALATED), None)
        if not run:
            return "No escalated run for this issue."
        stage = next((a for a in args if "=" not in a), None)
        from sdlc.config import parse_overrides
        overrides = parse_overrides([a for a in args if "=" in a])
        state = engine.resume(run.run_id, from_stage=stage, overrides=overrides or None)
        return f"{state.run_id}: resumed → {state.status.value}"

    if cmd == "abort":
        live = [r for r in runs_for_issue(engine.store, n) if r.status not in (RunStatus.DONE, RunStatus.ABORTED)]
        for r in live:
            engine.abort(r.run_id, f"/sdlc abort by {comment['author']} on issue #{n}")
        return f"aborted {len(live)} run(s)"

    if cmd == "status":
        lines = [f"- `{r.run_id}` ({r.pipeline}{' ' + r.inputs['ticket'] if r.inputs.get('ticket') else ''}): "
                 f"**{r.status.value}**" + (f" at {r.wait.gate}" if r.wait else "")
                 + (f" ({r.escalation['reason'][:160]})" if r.escalation else "") for r in runs_for_issue(engine.store, n)]
        github.comment(repo, n, "\n".join(lines) or "No runs for this issue yet.")
        return "status posted"

    github.comment(repo, n, HELP)
    return "help posted"


def handle_issue_commands(engine: Engine, repo: str, issues: list[dict], ci: dict) -> list[str]:
    actions = []
    for issue in issues:
        n = issue["number"]
        for c in github.issue_comments(repo, n):
            if c["id"] in ci["seen"] or not c["body"].strip().startswith("/sdlc"):
                continue
            ci["seen"].append(c["id"])
            if not _trusted(c):
                continue
            try:
                result = handle_command(engine, repo, n, c)
            except (EngineError, KeyError, ValueError) as exc:
                result = f"couldn't do that: {exc}"
                github.comment(repo, n, f"`{c['body'].strip()[:80]}`: {result}")
            actions.append(f"issue #{n} {c['body'].strip()[:40]!r}: {result}")
    return actions


# ---- updates back to the issue -------------------------------------------------------------------------

def _questions_md(info: dict) -> str:
    parts = []
    for q in info.get("questions", []):
        who = f" (for: {q['audience']})" if q.get("audience") else ""
        parts.append(f"**{q['id']}**{who}\n\n{q['text']}\n\n**Recommended:** {q.get('default') or '(none)'}")
    many = len(info.get("questions", [])) > 1
    how = ("Reply `/sdlc accept`, `/sdlc answer " + ("r1-q1: …` (one line per question)" if many else "<your answer>`")
           + ", or `/sdlc done` to stop asking (open points become assumptions).")
    return "\n\n---\n\n".join(parts) + f"\n\n{how}"


def notice(run: RunState) -> tuple[str, str] | None:
    """(milestone key, comment) for the run's current state, or None when there's nothing to say."""
    label = run.inputs.get("ticket") or "The pipeline"
    if run.status == RunStatus.WAITING_HUMAN and run.wait:
        info, gate = run.wait.info, run.wait.gate
        if gate == "h0_answers":
            return f"h0:{info.get('round', 1)}", "### Questions\n\n" + _questions_md(info)
        if gate == "h1_spec_review":
            prs = [p["pr"] for p in info.get("prs", [])] or [info.get("pr")]
            return "h1", ("### Spec ready for review\n\n" + "\n".join(f"- {p}" for p in prs if p)
                          + "\n\nComment on the files, then `/sdlc revise` on the PR. **Merge** it to approve; the "
                            "Jira stories are created after the merge.")
        if gate == "h3_pr_review":
            return "h3", (f"### {label}: code PR ready\n\n{info.get('pr')}\n\nReview it; `/sdlc fix` sends your "
                          "comments back, `/sdlc approve` adds the spec archive commit, then merge.")
        return None
    if run.status == RunStatus.ESCALATED and run.escalation:
        return (f"esc:{run.escalation.get('since')}",
                f"### {label} needs you\n\nStopped at `{run.escalation.get('at')}`: {run.escalation.get('reason')}\n\n"
                "`/sdlc resume` to try again (add `budgets.per_stage_usd.<stage>=N` to raise a budget), or `/sdlc abort`.")
    if run.status == RunStatus.DONE:
        if run.pipeline == "plan":
            published = next((Path(h.artifacts["published.json"]) for h in reversed(run.history)
                              if h.stage == "a4_publish" and "published.json" in h.artifacts), None)
            if published and published.exists():
                data = json.loads(published.read_text())
                stories = "\n".join(f"- [{c['jira']}]({c['url']}) `{c['id']}`" for c in data.get("changes", []))
                epic = f"[{data['epic']}]({data['epic_url']})" if data.get("epic") else "(no epic)"
                return "done", (f"### Stories created\n\nEpic {epic}\n\n{stories}\n\nStart building with "
                                "`/sdlc implement <KEY>` or `/sdlc implement all` (in dependency order).")
            return "done", "The plan run finished."
        return "done", f"{label}: merged, Jira moved to Done."
    if run.status == RunStatus.ABORTED:
        return "aborted", f"{label}: run `{run.run_id}` aborted."
    return None


def notify(engine: Engine, repo: str, ci: dict) -> list[str]:
    actions = []
    for run in engine.store.list():
        n = run.inputs.get("issue")
        if not n:
            continue
        found = notice(run)
        if not found:
            continue
        key, text = found
        sent = ci["notices"].setdefault(run.run_id, [])
        if key in sent:
            continue
        github.comment(repo, n, text)
        sent.append(key)
        actions.append(f"issue #{n}: posted {key} for {run.run_id}")
    return actions


# ---- the reconcile ----------------------------------------------------------------------------------------

def reconcile(repo: str, engine: Engine | None = None) -> list[str]:
    from sdlc import watch
    engine = engine or Engine(RunStore())
    ci = _load_ci()
    actions = orphaned(engine)
    try:
        ensure_registry(repo)
        issues = github.requirement_issues(repo, LABEL)
        for step in (start_new, apply_edits, handle_issue_commands):
            try:
                actions += step(engine, repo, issues, ci)
            finally:
                _save_ci(ci)
        actions += watch.poll_once(engine)
        actions += notify(engine, repo, ci)
    finally:
        _save_ci(ci)
    return actions


def summary(actions: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write("### sdlc reconcile\n\n" + ("\n".join(f"- {a}" for a in actions) or "- nothing to do") + "\n")
