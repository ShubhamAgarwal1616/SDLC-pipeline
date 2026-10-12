"""Real Pipeline B stages (P6): B5 review, B6 diff guard, B7 PR, B8 fix, B9 archive, B10 close.

The PR always contains exactly the commit that passed B4 verify, B5 review and B6 guard: B6 makes a clean
"candidate" clone of the agent's committed work, and B7 pushes that clone. Phase 1 never runs git inside the
agent's workspace.
"""

from __future__ import annotations

import fnmatch
import json
from pathlib import Path

from sdlc import gateway_keys, stacks
from sdlc.connectors import github, jira
from sdlc.engine.stage import StageContext, StageResult
from sdlc.sandbox import SandboxError
from sdlc.stages.pipeline_a import _agent_task, _run_agent, _run_dir, _runner
from sdlc.stages.publish import docs_status
from sdlc.stages.pipeline_b import (_prep, _ticket, current_base_sha, mount_cache, mount_contracts,
                                    sync_bundle)

BUILD_FILES = stacks.DEPENDENCY_FILES
MAX_DIFF_BYTES = 200_000


def _latest_verify_src(ctx: StageContext) -> Path:
    attempts = sorted((_run_dir(ctx) / "verify").glob("attempt-*"), key=lambda p: int(p.name.split("-")[1]))
    if not attempts:
        raise SandboxError("no verify checkout found; B4 must pass first")
    return attempts[-1] / "src"


def _clone_committed(ctx: StageContext, dest: Path) -> str:
    """A clean clone of the agent's committed HEAD (objects only; no hooks or config from the workspace)."""
    if dest.exists():
        import shutil
        shutil.rmtree(dest)
    dest.parent.mkdir(parents=True, exist_ok=True)
    github.git(None, "clone", "--quiet", "--no-hardlinks", str(_run_dir(ctx) / "workspace"), str(dest))
    return github.git(dest, "rev-parse", "HEAD").stdout.strip()


# ---- B5 AI review (LLM: /sdlc-code-reviewer, a different model) ---------------------------------------------

SEVERITY = {"blocker": 0, "major": 1, "minor": 2, "nit": 3}

FOLLOWUP = ("This is a follow-up review (round {round}). The previous review's findings are in "
            "/io/in/inputs/previous-review.json and the changes made since then are in /io/in/inputs/since-last.patch. "
            "First check each previous blocker/major finding and mark it in `previous` as fixed or not_fixed. Then "
            "raise a NEW blocker/major finding only if it is a regression introduced since the last review, or a "
            "security or correctness defect a careful reviewer could not have missed. Don't re-litigate style or "
            "add minor findings already covered. Re-check scenario coverage for the full change.")


def _reviewed_rounds(ctx: StageContext) -> list[dict]:
    """Earlier reviews in this run (any attempt), oldest first, with the commit each one reviewed."""
    out = []
    for h in ctx.run.history:
        if h.stage == ctx.stage and "review.json" in h.artifacts and Path(h.artifacts["review.json"]).exists():
            out.append(json.loads(Path(h.artifacts["review.json"]).read_text()))
    return out


class Review:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        ticket, prep = _ticket(ctx), _prep(ctx)
        src = _latest_verify_src(ctx)
        head = github.git(src, "rev-parse", "HEAD").stdout.strip()
        diff = github.git(src, "diff", f"{current_base_sha(ctx)}..HEAD").stdout
        inputs_dir = ctx.out_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        (inputs_dir / "diff.patch").write_text(diff[:MAX_DIFF_BYTES] + ("\n…(truncated)" if len(diff) > MAX_DIFF_BYTES else ""))
        inputs = {"diff.patch": inputs_dir / "diff.patch", "ticket.json": ctx.latest("b0_fetch", "ticket.json"),
                  "verify.json": ctx.latest("b4_verify", "verify.json")}
        if impl := ctx.latest("b3_implement", "impl-summary.json"):
            inputs["impl-summary.json"] = impl
        prompt = (f"/sdlc-code-reviewer Review the change for {ticket['key']} ({ticket['summary']}) against its "
                  f"approved spec in /workspace/openspec/changes/{ticket['change_id']}/. The diff is in "
                  f"/io/in/inputs/diff.patch. Follow the stage contract and write /io/out/review.json.")
        earlier = _reviewed_rounds(ctx)
        followup = bool(earlier) and ctx.config.get("review.followup_mode", True)
        if followup:
            last = earlier[-1]
            (inputs_dir / "previous-review.json").write_text(json.dumps(last, indent=2))
            inputs["previous-review.json"] = inputs_dir / "previous-review.json"
            since = github.git(src, "diff", f"{last.get('reviewed_sha')}..HEAD", check=False).stdout \
                if last.get("reviewed_sha") else ""
            (inputs_dir / "since-last.patch").write_text(since[:MAX_DIFF_BYTES] or "(not available: review the full diff)")
            inputs["since-last.patch"] = inputs_dir / "since-last.patch"
            prompt += " " + FOLLOWUP.format(round=len(earlier) + 1)
        task = _agent_task(ctx, prompt, "review.md", inputs, ["review.json"], None)   # fresh, independent session
        if followup and (cheaper := ctx.config.get("llm.stages.review.followup_model")):
            task.model = cheaper
        task.workspace, task.workspace_readonly = src, True
        mount_contracts(ctx, task)
        result = _run_agent(ctx, task)
        if not result.file("review.json").exists():
            raise SandboxError(f"review produced no review.json: {result.failure()}")
        review = json.loads(result.file("review.json").read_text())
        review["reviewed_sha"], review["round"], review["model"] = head, len(earlier) + 1, task.model
        blocking = sorted((f for f in review.get("findings", []) if f.get("severity") in {"blocker", "major"}),
                          key=lambda f: SEVERITY.get(f.get("severity"), 9))
        cap = ctx.config.get("review.max_blocking_per_round")
        deferred = blocking[cap:] if cap else []
        blocking = blocking[:cap] if cap else blocking
        not_fixed = [p for p in review.get("previous", []) if p.get("status") == "not_fixed"]
        gaps = [s for s in review.get("scenarios", []) if s.get("status") in {"missing", "untested"}]
        if deferred:
            review["deferred"] = deferred   # surfaced next round if still there
        artifacts = {"review.json": ctx.write("review.json", review)}
        if blocking or gaps or not_fixed or review.get("verdict") == "changes_required":
            lines = [f"- [{f['severity']}] {f.get('file')}:{f.get('line')} {f.get('rule') or ''} {f['issue']} "
                     f"→ fix: {f.get('fix')}" for f in blocking]
            lines += [f"- still not fixed from the last review: {p.get('issue') or p.get('id')}" for p in not_fixed]
            lines += [f"- scenario '{s['name']}' is {s['status']}: add a test that asserts it" for s in gaps]
            return StageResult("blocking", artifacts, note="\n".join(lines) or review.get("summary"), usage=result.usage)
        return StageResult("ok", artifacts, note=review.get("summary"), usage=result.usage)


# ---- B6 diff guard (script) ---------------------------------------------------------------------------------

def added_dependencies(base_text: str, head_text: str, path: str = "build.gradle.kts") -> list[str]:
    return stacks.added_dependencies(path, base_text, head_text)


class Guard:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        prep = _prep(ctx)
        cand = _run_dir(ctx) / "candidate"
        sha = _clone_committed(ctx, cand)
        base = current_base_sha(ctx)
        numstat = github.git(cand, "diff", "--numstat", f"{base}..HEAD").stdout.splitlines()
        files, lines = [], 0
        for row in numstat:
            add, delete, path = row.split("\t", 2)
            files.append(path)
            lines += (int(add) if add.isdigit() else 0) + (int(delete) if delete.isdigit() else 0)
        violations = []
        for path in files:
            for pattern in ctx.config.get("guardrails.forbidden_paths", []):
                if fnmatch.fnmatch(path, pattern) or path == pattern.rstrip("/*"):
                    violations.append({"rule": "forbidden_path", "path": path, "pattern": pattern})
        if lines > ctx.config.get("guardrails.max_diff_lines", 800):
            violations.append({"rule": "max_diff_lines", "value": lines})
        if len(files) > ctx.config.get("guardrails.max_files_changed", 25):
            violations.append({"rule": "max_files_changed", "value": len(files)})
        if not ctx.config.get("guardrails.allow_new_dependencies", False):
            for build_file in (f for f in files if f in BUILD_FILES or f.endswith(BUILD_FILES)):
                before = github.git(cand, "show", f"{base}:{build_file}", check=False).stdout
                after = (cand / build_file).read_text() if (cand / build_file).exists() else ""
                for dep in added_dependencies(before, after, build_file):
                    violations.append({"rule": "new_dependency", "path": build_file, "line": dep})
        report = {"passed": not violations, "sha": sha, "files": files, "lines_changed": lines, "violations": violations}
        artifacts = {"guard.json": ctx.write("guard.json", report)}
        if violations:
            return StageResult("violation", artifacts, note="; ".join(f"{v['rule']}: {v.get('path') or v.get('value')}"
                                                                   for v in violations[:8]))
        return StageResult("pass", artifacts, note=f"{len(files)} files, {lines} lines")


# ---- B7 push + PR (script) -------------------------------------------------------------------------------------

def _pr_body(ctx: StageContext, ticket: dict, guard: dict) -> str:
    review = json.loads(ctx.latest("b5_review", "review.json").read_text()) if ctx.latest("b5_review", "review.json") else {}
    verify = json.loads(ctx.latest("b4_verify", "verify.json").read_text())
    impl = json.loads(ctx.latest("b3_implement", "impl-summary.json").read_text()) if ctx.latest(
        "b3_implement", "impl-summary.json") else {}
    rows = "\n".join(f"| {s['name']} | {s['status']} | {', '.join(s.get('tests') or []) or '–'} |"
                     for s in review.get("scenarios", [])) or "| (no review) | | |"
    minors = "\n".join(f"- [{f['severity']}] `{f.get('file')}:{f.get('line')}` {f['issue']}"
                       for f in review.get("findings", []) if f.get("severity") in {"minor", "nit"}) or "- none"
    t = verify.get("tests", {})
    return (f"## {ticket['key']}: {ticket['summary']}\n\n{impl.get('summary', '')}\n\n"
            f"Jira: [{ticket['key']}]({ticket['url']}) · Spec: `openspec/changes/{ticket['change_id']}/`\n\n"
            f"### Scenario coverage (AI review)\n| Scenario | Status | Tests |\n|---|---|---|\n{rows}\n\n"
            f"### Verify (clean, offline container)\n"
            f"tests {t.get('tests', 0)} · failures {t.get('failures', 0)} · checks "
            + ", ".join(f"{k} {'✅' if v == 'pass' else '❌'}" for k, v in verify.get("checks", {}).items())
            + f"\n\n### Minor review notes\n{minors}\n\n"
            f"Diff: {len(guard['files'])} files, {guard['lines_changed']} lines · "
            f"Run cost so far: ${gateway_keys.run_spent(ctx.run.run_id):.2f}\n\n"
            "**How to review:** comment on the code, then `/sdlc fix` to send comments to the pipeline. "
            "`/sdlc approve` adds the OpenSpec archive commit; then merge. `/sdlc abort` stops the run."
            f"\n\n{github.BOT_MARKER}")


class RaisePr:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        ticket, prep = _ticket(ctx), _prep(ctx)
        guard = json.loads(ctx.latest("b6_guard", "guard.json").read_text())
        cand = _run_dir(ctx) / "candidate"
        if github.git(cand, "rev-parse", "HEAD").stdout.strip() != guard["sha"]:
            raise SandboxError("candidate checkout changed after the diff guard; refusing to push")
        repo, branch = prep["repo"], prep["branch"]
        if ctx.run.inputs.get("dry_run"):
            return StageResult("ok", note="dry run: nothing pushed", gate_info={"review": "dry run"})
        url = f"https://github.com/{repo}.git"
        push = github.git(cand, "push", "--quiet", url, f"HEAD:refs/heads/{branch}", check=False)
        if push.returncode != 0:
            # Never force: someone else's commits may be on the branch. B8/B3 merge them via a sync bundle.
            raise SandboxError(f"push to {branch} was rejected (not a fast-forward): a person probably pushed to the "
                               f"PR branch. Resume from b8_fix (or b3_implement) and the agent will merge their "
                               f"commits first. Details: {push.stderr.strip()[-200:]}")
        pr = github.open_or_update_pr(repo, branch, prep["base"], f"{ticket['key']}: {ticket['summary']}",
                                      _pr_body(ctx, ticket, guard))
        try:
            github.add_labels(repo, pr["number"], ["ai-generated"])
        except github.GitHubError:
            pass
        replies_file = ctx.latest("b8_fix", "replies.json")
        posted = 0
        if replies_file and ctx.attempt > 1:
            for reply in json.loads(replies_file.read_text()).get("replies", []):
                cid = str(reply.get("comment_id", ""))
                try:
                    if cid.startswith("rc-"):
                        github.request("POST", f"/repos/{repo}/pulls/{pr['number']}/comments/{cid[3:]}/replies",
                                       {"body": f"{reply['body']}\n\n{github.BOT_MARKER}"})
                    else:
                        github.comment(repo, pr["number"], f"Re {cid}: {reply['body']}")
                    posted += 1
                except github.GitHubError:
                    pass
        if ctx.attempt == 1:
            if target := ctx.config.get("publish.transitions.review", "In Review"):
                jira.transition(ticket["key"], target)
            jira.request("POST", f"/rest/api/3/issue/{ticket['key']}/comment",
                         {"body": jira.to_adf(f"Pull request opened by the pipeline: [{pr['html_url']}]({pr['html_url']})")})
            docs_status(ctx, ticket["change_id"], f"In review · PR {pr['html_url']} · Jira {ticket['key']}")
        info = {"pr": pr["html_url"], "pr_number": pr["number"], "repo": repo,
                "how": "comment /sdlc fix after leaving comments; /sdlc approve then merge (run `sdlc watch`)"}
        return StageResult("ok", {"pr.json": ctx.write("pr.json", {**info, "sha": guard["sha"], "replies_posted": posted})},
                           note=f"PR #{pr['number']} at {guard['sha'][:8]}", gate_info=info)


# ---- B8 fix PR comments (LLM: /sdlc-feedback-resolver, resuming the implement session) ---------------------

class Fix:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        ticket = _ticket(ctx)
        events = [f for f in ctx.feedback if f.get("source") == "h3_pr_review"]
        data = events[-1].get("data", {}) if events else {}
        comments = [{k: c.get(k) for k in ("id", "author", "path", "line", "body")} for c in data.get("comments", [])]
        if data.get("text") and not comments:
            comments = [{"id": "cli", "author": "reviewer", "path": None, "line": None, "body": data["text"]}]
        inputs_dir = ctx.out_dir / "inputs"
        inputs_dir.mkdir(parents=True, exist_ok=True)
        (inputs_dir / "comments.json").write_text(json.dumps(comments, indent=2))
        bundle, sync_text = sync_bundle(ctx)
        prompt = sync_text + (f"/sdlc-feedback-resolver Address the human review comments on the PR for {ticket['key']} "
                  f"(in /io/in/inputs/comments.json; extra note: {data.get('text') or 'none'}). Follow the stage contract.")
        fix_inputs = {"comments.json": inputs_dir / "comments.json"}
        if bundle:
            fix_inputs["sync.bundle"] = bundle
        task = _agent_task(ctx, prompt, "fix.md", fix_inputs, ["replies.json"], ctx.run.sessions.get("b3_implement"))
        mount_cache(ctx, _prep(ctx), task)
        mount_contracts(ctx, task)
        result = _run_agent(ctx, task)
        if result.data.get("session_id"):
            ctx.run.sessions["b3_implement"] = result.data["session_id"]   # keep one implementation thread
        if not result.file("replies.json").exists():
            raise SandboxError(f"fix produced no replies.json: {result.failure()}")
        replies = json.loads(result.file("replies.json").read_text())
        artifacts = {"replies.json": ctx.write("replies.json", replies)}
        if (ctx.out_dir / "sync.json").exists():
            artifacts["sync.json"] = str(ctx.out_dir / "sync.json")
        return StageResult("ok", artifacts,
                           note=replies.get("summary"), usage=result.usage)


# ---- B9 archive (script + OpenSpec CLI in a tool container) and B10 close --------------------------------------

class Archive:
    """On approval: add the `openspec archive` commit to the PR branch (pre-merge), or if the PR was
    already merged, open a small follow-up PR with the archive."""

    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        ticket, prep = _ticket(ctx), _prep(ctx)
        repo, cid = prep["repo"], ticket["change_id"]
        pr_info = json.loads(ctx.latest("b7_pr", "pr.json").read_text())
        pr = github.get_pr(repo, pr_info["pr_number"])
        merged = bool(pr.get("merged"))
        pub = _run_dir(ctx) / "archive"
        if pub.exists():
            import shutil
            shutil.rmtree(pub)
        github.clone(repo, pub, depth=None)
        branch = prep["branch"] if not merged else f"chore/archive-{cid}"[:100]
        start = f"origin/{prep['branch']}" if not merged else f"origin/{prep['base']}"
        github.git(pub, "checkout", "--quiet", "-B", branch, start)
        if not (pub / "openspec" / "changes" / cid).is_dir():
            return StageResult("archived" if not merged else "closed_followup", note=f"{cid} already archived")
        proc = _runner(ctx).run_tool(pub, ["openspec", "archive", cid, "-y", "--json"], writable=True)
        if proc.returncode != 0:
            raise SandboxError(f"openspec archive failed: {(proc.stdout + proc.stderr).strip()[-400:]}")
        github.git(pub, "add", "-A")
        github.git(pub, "commit", "--quiet", "-m", f"chore(spec): archive {cid} ({ticket['key']})")
        github.git(pub, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")
        artifacts = {"archive.json": ctx.write("archive.json", {"merged_before_archive": merged, "branch": branch,
                                                                "openspec": proc.stdout[-2000:]})}
        if merged:
            follow = github.open_or_update_pr(repo, branch, prep["base"], f"chore(spec): archive {cid}",
                                              f"Archives the OpenSpec change for {ticket['key']} (merged in "
                                              f"#{pr['number']}).\n\n{github.BOT_MARKER}")
            return StageResult("closed_followup", artifacts, note=f"archive follow-up PR #{follow['number']}")
        github.comment(repo, pr["number"], f"Approved: added the OpenSpec archive commit for `{cid}` "
                                           "(living specs updated). Ready to merge.")
        return StageResult("archived", artifacts, note="archive commit added; waiting for merge",
                           gate_info={**{k: pr_info[k] for k in ("pr", "pr_number", "repo")},
                                      "how": "merge the PR (sdlc watch moves Jira to Done)"})


class Close:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        ticket = _ticket(ctx)
        target = ctx.config.get("publish.transitions.done", "Done")
        moved = jira.transition(ticket["key"], target) if target else False
        pr = ctx.latest("b7_pr", "pr.json")
        pr_url = json.loads(pr.read_text()).get("pr") if pr else None
        docs_status(ctx, ticket["change_id"], f"Done · merged{' in ' + pr_url if pr_url else ''} · Jira {ticket['key']}")
        return StageResult("ok", {"close.json": ctx.write("close.json", {"jira": ticket["key"], "moved_to": target if moved else None})},
                           note=f"{ticket['key']} → {target}" if moved else "Jira transition not available")
