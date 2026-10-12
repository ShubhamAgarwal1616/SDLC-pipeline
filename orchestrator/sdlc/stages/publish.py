"""Real publishing stages (P4): the spec PR (H1's review surface) and A4 publish to Jira.

Phase 1 never runs git in the agent's workspace (the agent could plant hooks or config there).
It keeps its own clone, `runs/<id>/publish`, copies only allow-listed regular files into it,
and commits and pushes from there with hooks disabled.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from sdlc.connectors import confluence, github, jira
from sdlc.engine.stage import StageContext, StageResult
from sdlc.openspec_parse import sections, story_markdown
from sdlc.sandbox import SandboxError
from sdlc.registry import Target, plan_targets
from sdlc.stages.pipeline_a import listed_changes

MAX_FILE_BYTES = 1_000_000


def safe_copy_tree(src: Path, dest: Path) -> list[str]:
    """Copy regular files only. Symlinks and oversized files are skipped, never followed."""
    copied = []
    for path in sorted(src.rglob("*")):
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_FILE_BYTES:
            continue
        target = dest / path.relative_to(src)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, target)
        copied.append(str(target))
    return copied


def _clone_at(pub: Path, repo: str) -> Path:
    if not (pub / ".git").exists():
        pub.parent.mkdir(parents=True, exist_ok=True)
        github.clone(repo, pub, depth=None)
    github.git(pub, "fetch", "--quiet", "origin")
    return pub


def _publish_clone(ctx: StageContext, repo: str, base: str) -> Path:
    return _clone_at(ctx.store.run_dir(ctx.run.run_id) / "publish", repo)


def _base_branch(ctx: StageContext, repo: str, feature: bool = False) -> str:
    if not feature and ctx.config.get("project.github.base_branch"):
        return ctx.config.get("project.github.base_branch")
    return github.repo_info(repo)["default_branch"]


def requirement_title(ctx: StageContext) -> str:
    first = Path(ctx.run.inputs["requirements"]).read_text().strip().splitlines()[:1]
    return (first[0].lstrip("# ").strip() if first else "") or ctx.run.run_id


def _targets(ctx: StageContext) -> list[Target]:
    return plan_targets(ctx.store.run_dir(ctx.run.run_id), ctx.run.inputs)


class SpecPr:
    """Commit each repo's proposal to spec/<run-id> and open (or update) its spec PR. Feature runs get one
    PR per repo with changes, cross-linked; the gate approves when all of them are merged."""

    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        targets = _targets(ctx)
        if not any(t.github for t in targets):
            return StageResult("ok", note="no --repo: review locally with `sdlc review`",
                               gate_info={"review": "local only (no --repo given)"})
        if ctx.run.inputs.get("dry_run"):
            return StageResult("ok", note="dry run: no branch pushed", gate_info={"review": "dry run"})
        feature = len(targets) > 1
        prs = [pr for t in targets if (pr := self._one(ctx, t, feature))]
        if not prs:
            raise SandboxError("no repo has any change to propose")
        if len(prs) > 1:   # cross-link the feature's spec PRs
            links = "\n".join(f"- {p['repo_name']}: {p['html_url']}" for p in prs)
            for p in prs:
                github.request("PATCH", f"/repos/{p['repo']}/pulls/{p['number']}",
                               {"body": p["body"].replace(github.BOT_MARKER, "")
                                + f"\n\n**Feature spec PRs** (approve by merging all of them):\n{links}\n\n{github.BOT_MARKER}"})
        how = "comment /sdlc revise to send review comments; merge to approve (run `sdlc watch`)"
        if feature:
            info = {"prs": [{"repo": p["repo"], "pr_number": p["number"], "pr": p["html_url"], "name": p["repo_name"]}
                            for p in prs], "how": how.replace("merge to approve", "merge ALL of them to approve")}
        else:
            info = {"pr": prs[0]["html_url"], "pr_number": prs[0]["number"], "repo": prs[0]["repo"], "how": how}
        pushed = [p for p in prs if p["pushed"]]
        artifacts = {"spec-pr.json": ctx.write("spec-pr.json", {**info, "pushed": [p["repo"] for p in pushed]})}
        return StageResult("ok", artifacts, note=None if pushed else "no file changes to push", gate_info=info)

    def _one(self, ctx: StageContext, t: Target, feature: bool) -> dict | None:
        changes_file = ctx.latest("a2_propose", t.prefix + "changes.json")
        listed = listed_changes(changes_file)
        if feature and not listed:
            return None   # this repo has no part in the feature
        changes = json.loads(changes_file.read_text())["changes"]
        repo, run_id = t.github, ctx.run.run_id
        base = _base_branch(ctx, repo, feature)
        branch = f"spec/{run_id}"
        pub = _clone_at(t.publish, repo)
        has_remote = github.git(pub, "rev-parse", "--verify", "--quiet", f"origin/{branch}", check=False).returncode == 0
        github.git(pub, "checkout", "--quiet", "-B", branch, f"origin/{branch}" if has_remote else f"origin/{base}")

        pub_changes = pub / "openspec" / "changes"
        if pub_changes.exists():
            for old in pub_changes.iterdir():
                if old.is_dir() and old.name != "archive" and old.name not in listed:
                    shutil.rmtree(old)
        for cid in sorted(listed):
            if (pub_changes / cid).exists():
                shutil.rmtree(pub_changes / cid)
            safe_copy_tree(t.ws / "openspec" / "changes" / cid, pub_changes / cid)
        if prd := ctx.latest("a2_propose", "prd.md"):
            (pub / "docs" / "prd").mkdir(parents=True, exist_ok=True)
            shutil.copyfile(prd, pub / "docs" / "prd" / f"{run_id}.md")
        if (t.ws / "CONTEXT.md").is_file() and not (t.ws / "CONTEXT.md").is_symlink():
            shutil.copyfile(t.ws / "CONTEXT.md", pub / "CONTEXT.md")
        if (t.ws / "docs" / "adr").is_dir():
            safe_copy_tree(t.ws / "docs" / "adr", pub / "docs" / "adr")

        github.git(pub, "add", "-A")
        changed = github.git(pub, "diff", "--cached", "--quiet", check=False).returncode != 0
        title = requirement_title(ctx)
        if changed:
            rev = "" if ctx.attempt == 1 else f" (revision {ctx.attempt - 1})"
            github.git(pub, "commit", "--quiet", "-m", f"spec: {title}{rev}\n\nPipeline run {run_id}: "
                                                      f"{len(changes)} OpenSpec change(s).")
            github.git(pub, "push", "--quiet", "origin", f"HEAD:refs/heads/{branch}")

        table = "\n".join(f"| `{c['id']}` | {c['title']} | {', '.join(c.get('depends_on') or []) or '–'} |"
                          for c in changes)
        scope = f" for **{t.name}** (part of a multi-repo feature)" if feature else ""
        body = (f"Spec proposal{scope} from pipeline run `{run_id}`.\n\n| Change | Title | Depends on |\n|---|---|---|\n"
                f"{table}\n\nPRD: `docs/prd/{run_id}.md` · Glossary: `CONTEXT.md`\n\n"
                "**How to review:** leave comments on the files, then comment `/sdlc revise` to send them to the "
                "pipeline. **Merge** this PR to approve; Jira stories are created after merge. `/sdlc abort` stops the run."
                f"\n\n{github.BOT_MARKER}")
        pr = github.open_or_update_pr(repo, branch, base, f"spec: {title} ({len(changes)} changes)", body)
        try:
            github.add_labels(repo, pr["number"], ["ai-spec"])
        except github.GitHubError:
            pass
        if ctx.attempt > 1 and changed:
            github.comment(repo, pr["number"], "Revised the proposal to address the latest review comments.")
        return {**pr, "repo": repo, "repo_name": t.name or repo, "body": body, "pushed": changed}


def mirror_docs(ctx: StageContext, title: str, prd_md: str, plan: dict, roots: dict, keys: dict,
                epic_key: str | None) -> dict:
    """The Confluence mirror: a page for the requirement (PRD + stories) and a child page per change."""
    conf = confluence.client(ctx.config)
    if conf is None:
        return {}
    try:
        rows = "\n".join(f"| `{st['id']}` | {st['summary']} | {keys[(st['repo'], st['id'])]} |"
                         + (f" {st['repo']} |" if st["repo"] else "") for st in plan["stories"])
        repo_col = " Repo |" if any(st["repo"] for st in plan["stories"]) else ""
        sep = "---|" if repo_col else ""
        md = (prd_md or f"# {title}") + f"\n\n## Stories\n\n| Change | Title | Jira |{repo_col}\n|---|---|---|{sep}\n{rows}\n"
        parent = conf.upsert(f"{title} [{epic_key or ctx.run.run_id}]",
                             confluence.with_status(confluence.to_storage(md), "Spec approved; stories are in Jira"),
                             f"sdlc-{ctx.run.run_id}")
        pages = [parent]
        for st in plan["stories"]:
            change_dir = roots[st["repo"] or ""][0] / "openspec" / "changes" / st["id"]
            key = keys[(st["repo"], st["id"])]
            body = story_markdown(st["id"], change_dir, roots[st["repo"] or ""][1] + f"/openspec/changes/{st['id']}")
            if (change_dir / "design.md").is_file():
                body += "\n\n# Design\n\n" + (change_dir / "design.md").read_text()
            pages.append(conf.upsert(f"{st['summary']} [{key}]",
                                     confluence.with_status(confluence.to_storage(body), f"To do · Jira {key}"),
                                     f"openspec-{st['id']}", parent["id"]))
        return {"pages": pages}
    except (confluence.ConfluenceError, OSError) as exc:   # the mirror never blocks the Jira backlog
        return {"error": str(exc)[:300]}


def docs_status(ctx: StageContext, change_id: str, status: str) -> str | None:
    """Update a change page's status line (B7 PR opened, B10 done). Best effort; returns a note on failure."""
    try:
        conf = confluence.client(ctx.config)
        found = conf.storage_of(f"openspec-{change_id}") if conf else None
        if found:
            page, storage = found
            conf.upsert(page["title"], confluence.with_status(storage, status), f"openspec-{change_id}",
                        page.get("parent") or None)
        return None
    except (confluence.ConfluenceError, OSError) as exc:
        return f"confluence: {str(exc)[:200]}"


class Publish:
    """A4: one Epic for the requirement/feature and one Story per merged change (per repo), with Blocks links:
    within a repo from depends_on, and across repos from config/repos.yaml (providers block consumers). Idempotent."""

    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        project = ctx.config.get("project.jira.project_key")
        if not project:
            raise SandboxError("project.jira.project_key is not set (in .sdlc/pipeline.yaml)")
        run_id = ctx.run.run_id
        targets = _targets(ctx)
        feature = len(targets) > 1
        title = ctx.config.get("publish.epic_title") or requirement_title(ctx)
        labels = ctx.config.get("publish.labels", ["ai-pipeline"])
        plan = {"project": project, "epic": {"summary": title, "label": f"sdlc-{run_id}"}, "stories": []}
        roots: dict[str, tuple[Path, str]] = {}
        for t in targets:
            changes = json.loads(ctx.latest("a2_propose", t.prefix + "changes.json").read_text())["changes"]
            if t.github:   # publish what was actually merged (humans may have edited the spec PR)
                base = _base_branch(ctx, t.github, feature)
                pub = _clone_at(t.publish, t.github)
                github.git(pub, "checkout", "--quiet", "--detach", f"origin/{base}")
                roots[t.name or ""] = (pub, f"https://github.com/{t.github}/tree/{base}")
            else:
                roots[t.name or ""] = (t.ws, "")
            root = roots[t.name or ""][0]
            missing = [c["id"] for c in changes if not (root / "openspec" / "changes" / c["id"]).is_dir()]
            if missing:
                raise SandboxError(f"changes not on the base branch of {t.github or 'the workspace'} "
                                   f"(was the spec PR merged?): {missing}")
            for c in changes:
                plan["stories"].append({"id": c["id"], "repo": t.name, "github": t.github, "summary": c["title"],
                                        "label": f"openspec-{c['id']}", "depends_on": c.get("depends_on") or [],
                                        "repo_depends_on": t.info.get("depends_on", []) or []})
        if ctx.run.inputs.get("dry_run"):
            if ctx.config.get("project.docs") == "confluence":
                plan["confluence"] = {"space": ctx.config.get("confluence.space"),
                                      "pages": [title] + [st["summary"] for st in plan["stories"]]}
            return StageResult("ok", {"publish-plan.json": ctx.write("publish-plan.json", plan)},
                               note="dry run: nothing created in Jira or Confluence")

        prd_path = ctx.latest("a2_propose", "prd.md")
        prd = sections(prd_path.read_text()) if prd_path else {}
        epic_key = None
        if ctx.config.get("publish.epic", True):
            found = jira.find_by_label(project, plan["epic"]["label"])
            epic_md = "\n\n".join(f"## {h}\n{prd[h]}" for h in ("Problem Statement", "Solution") if prd.get(h))
            if feature:
                epic_md += "\n\n## Repos\n" + "\n".join(f"- {t.name}: `{t.github}`" for t in targets)
            epic_key = found[0]["key"] if found else jira.create_issue(
                project, ctx.config.get("publish.epic_type", "Epic"), title, epic_md, [plan["epic"]["label"], *labels])
        keys: dict[tuple, str] = {}
        for st in plan["stories"]:
            found = jira.find_by_label(project, st["label"])
            if found:
                keys[(st["repo"], st["id"])] = found[0]["key"]
                continue
            root, url_base = roots[st["repo"] or ""]
            url = f"{url_base}/openspec/changes/{st['id']}" if url_base else f"openspec/changes/{st['id']}"
            body = story_markdown(st["id"], root / "openspec" / "changes" / st["id"], url)
            if st["github"]:
                body = f"**Repo:** `{st['github']}`\n\n" + body
            story_labels = [st["label"], *labels] + ([f"repo-{st['repo']}"] if st["repo"] else [])
            keys[(st["repo"], st["id"])] = jira.create_issue(project, ctx.config.get("publish.story_type", "Story"),
                                                             st["summary"], body, story_labels, parent=epic_key)
        for st in plan["stories"]:
            mine = keys[(st["repo"], st["id"])]
            existing = {(lk.get("outwardIssue") or lk.get("inwardIssue") or {}).get("key") for lk in jira.links(mine)}
            blockers = [keys[(st["repo"], d)] for d in st["depends_on"] if (st["repo"], d) in keys]
            blockers += [k for (repo, _), k in keys.items() if repo in st["repo_depends_on"]]   # providers block consumers
            for blocker in blockers:
                if blocker != mine and blocker not in existing:
                    jira.link_blocks(blocker=blocker, blocked=mine)
                    existing.add(blocker)
        docs = mirror_docs(ctx, title, prd_path.read_text() if prd_path else "", plan, roots, keys, epic_key)
        published = {"epic": epic_key, "epic_url": jira.browse_url(epic_key) if epic_key else None,
                     "changes": [{"id": cid, "repo": repo, "jira": key, "url": jira.browse_url(key)}
                                 for (repo, cid), key in keys.items()], "docs": docs}
        note = f"Jira: {epic_key + ' + ' if epic_key else ''}{', '.join(keys.values())}"
        if docs.get("pages"):
            note += f"; Confluence: {docs['pages'][0]['url']}"
        elif docs.get("error"):
            note += f"; Confluence failed ({docs['error'][:120]})"
        return StageResult("ok", {"published.json": ctx.write("published.json", published)}, note=note)
