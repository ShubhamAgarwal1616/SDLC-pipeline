"""`sdlc init`: make a GitHub repo ready for the pipeline (PLAN.md §17). No Docker needed.

Adds, where missing: .sdlc/pipeline.yaml, openspec/ (+ OpenSpec's Claude skills; a bundled copy of what
`openspec init --tools claude` writes), CLAUDE.md, README.md, optionally a starter service skeleton, and the
GitHub Actions workflow + "Feature request (SDLC)" issue form that run the pipeline in the repo. An empty repo
gets the first commit pushed to its base branch; a repo with history gets a PR. Re-running it updates the
workflow to this CLI's version and leaves everything else alone.
"""

from __future__ import annotations

import re
import shutil
from pathlib import Path

import yaml

from sdlc.config import PACKAGE_DIR, RUNS, expand, load_config, version
from sdlc.connectors import github
from sdlc.stacks import detect as detect_stack

TEMPLATES = PACKAGE_DIR / "templates"
SKELETONS = sorted(p.name for p in (TEMPLATES / "skeleton").iterdir() if p.is_dir())
WORKFLOW = Path(".github/workflows/sdlc.yml")
ISSUE_FORM = Path(".github/ISSUE_TEMPLATE/sdlc-feature.yml")
RELAY = Path(".github/workflows/sdlc-relay.yml")
LABELS = {"sdlc": ("5319e7", "Feature request handled by the SDLC pipeline"),
          "ai-spec": ("c5def5", "Spec proposal written by the SDLC pipeline"),
          "ai-generated": ("fbca04", "Code written by the SDLC pipeline")}

CLAUDE_STUB = """# Project conventions
Short, always-loaded notes for the AI pipeline and for people. Keep this under ~2k tokens.
- Domain glossary: see `CONTEXT.md`. Decisions: `docs/adr/`. Specs: `openspec/specs/`.
"""


INSTRUCTIONS_README = """# Stage instructions for the SDLC pipeline

Optional. A file here named after a stage is appended to that stage's contract, so the AI follows this repo's
own rules on top of the org's. Keep each short (under 8 KB): every run reads it.

| File | Stage |
|---|---|
| `grill.md` | asking clarifying questions about a requirement |
| `propose.md` | writing the PRD and the OpenSpec changes |
| `implement.md` | writing the code (test-first) |
| `review.md` | the AI code review |
| `fix.md` | addressing PR review comments |

Example `implement.md`: "Persist through the `OrderRepository` port only. Money is `BigDecimal` with scale 2."
Changes take effect for runs started after they're merged.
"""


def _fill(text: str, values: dict[str, str]) -> str:
    return re.sub(r"\{\{(\w+)\}\}", lambda m: values.get(m.group(1), m.group(0)), text)


def copy_template(src: Path, dest: Path, values: dict[str, str], overwrite: bool = False) -> list[str]:
    """Copy a template tree: {{key}} filled in text files, a `__package__` path segment becomes the package
    path. Existing files are kept unless overwrite. Returns the paths written, relative to dest."""
    written = []
    for path in sorted(src.rglob("*")):
        if not path.is_file() or path.name == ".DS_Store":
            continue
        rel = str(path.relative_to(src)).replace("__package__", values.get("package_path", "__package__"))
        target = dest / rel
        if target.exists() and not overwrite:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        data = path.read_bytes()
        try:
            target.write_text(_fill(data.decode(), values))
        except UnicodeDecodeError:   # binary (gradle-wrapper.jar)
            target.write_bytes(data)
        target.chmod(path.stat().st_mode)
        written.append(rel)
    return written


def pipeline_config(repo: str, base: str, jira_project: str | None, ws: Path,
                    confluence_space: str | None = None) -> dict:
    cfg: dict = {"pipeline_version": version(),
                 "project": {"scm": "github", "github": {"repo": repo, "base_branch": base},
                             "tracker": "jira", "jira": {"project_key": jira_project},
                             "docs": "confluence" if confluence_space else "none"},
                 "publish": {"epic": True}}
    if confluence_space:
        cfg["confluence"] = {"space": confluence_space}
    if stack := detect_stack(ws):
        cfg["project"]["stack"] = stack.name
        cfg["verify"] = {"commands": list(stack.verify), "build_files": list(stack.build_files)}
        if stack.skill_hints:
            cfg["skills"] = {"hints": [dict(h) for h in stack.skill_hints]}
    return cfg


def package_name(repo: str) -> str:
    owner, name = (re.sub(r"[^a-z0-9]", "", part.lower()) or "app" for part in repo.split("/", 1))
    return f"com.{owner}.{name}" if not owner[0].isdigit() else f"com.app.{name}"


def scaffold(ws: Path, repo: str, base: str, jira_project: str | None, skeleton: str | None = None,
             package: str | None = None, ci: bool = True, confluence_space: str | None = None,
             relay_to: str | None = None, registry_file: Path | None = None) -> list[str]:
    """relay_to: this repo is a member of multi-repo features run from that home repo (adds only the relay
    workflow). registry_file: this repo is a feature home; the file becomes .sdlc/repos.yaml."""
    """Write the pipeline's files into a local checkout `ws`. Returns what was created or updated."""
    created: list[str] = []
    name = repo.split("/", 1)[1]
    if skeleton:
        if skeleton not in SKELETONS:
            raise ValueError(f"unknown skeleton {skeleton!r}; available: {SKELETONS}")
        if any((ws / f).exists() for f in ("build.gradle.kts", "build.gradle", "pom.xml", "package.json")):
            raise ValueError("the repo already has a build; a skeleton is only for an empty project")
        package = package or package_name(repo)
        values = {"name": name, "package": package, "group": package.rsplit(".", 1)[0],
                  "package_path": package.replace(".", "/"),
                  "npm_name": re.sub(r"[^a-z0-9._-]", "-", name.lower()).strip("-._") or "app"}
        created += [f"skeleton: {p}" for p in copy_template(TEMPLATES / "skeleton" / skeleton, ws, values)]
    cfg_path = ws / ".sdlc" / "pipeline.yaml"
    if not cfg_path.exists():
        cfg_path.parent.mkdir(parents=True, exist_ok=True)
        cfg_path.write_text("# Project layer of the pipeline config (PLAN.md §15).\n"
                            + yaml.safe_dump(pipeline_config(repo, base, jira_project, ws, confluence_space),
                                             sort_keys=False))
        created.append(".sdlc/pipeline.yaml")
    readme = ws / ".sdlc" / "instructions" / "README.md"
    if not readme.exists():
        readme.parent.mkdir(parents=True, exist_ok=True)
        readme.write_text(INSTRUCTIONS_README)
        created.append(".sdlc/instructions/README.md")
    for fname, text in {"CLAUDE.md": CLAUDE_STUB, "README.md": f"# {name}\n"}.items():
        if not (ws / fname).exists():
            (ws / fname).write_text(text)
            created.append(fname)
    if not (ws / "openspec").exists():
        copy_template(TEMPLATES / "openspec", ws, {})
        created.append("openspec/ (+ .claude/ OpenSpec skills)")
    if registry_file:
        from sdlc import registry
        registry.load(Path(registry_file))      # validates names and depends_on
        target = ws / ".sdlc" / "repos.yaml"
        text = Path(registry_file).read_text()
        if not target.exists() or target.read_text() != text:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text)
            created.append(".sdlc/repos.yaml")
    if ci:
        install = expand(load_config(env={}).get("distribution.install"))
        values = {"version": version(), "install": install, "home": relay_to or repo}
        files = ([(TEMPLATES / "github" / "sdlc-relay.yml", RELAY)] if relay_to else
                 [(TEMPLATES / "github" / "sdlc.yml", WORKFLOW), (TEMPLATES / "github" / "sdlc-feature.yml", ISSUE_FORM)])
        for src, dest in files:
            before = (ws / dest).read_text() if (ws / dest).exists() else None
            after = _fill(src.read_text(), values)
            if before != after:
                (ws / dest).parent.mkdir(parents=True, exist_ok=True)
                (ws / dest).write_text(after)
                created.append(f"{dest}{' (updated)' if before is not None else ''}")
    return created


def init_repo(repo: str, jira_project: str | None, base: str | None = None, skeleton: str | None = None,
              package: str | None = None, ci: bool = True, confluence_space: str | None = None,
              relay_to: str | None = None, registry_file: Path | None = None) -> dict:
    info = github.repo_info(repo)
    base = base or info.get("default_branch") or "main"
    work = RUNS / "_init" / repo.replace("/", "__")
    if work.exists():
        shutil.rmtree(work)
    work.parent.mkdir(parents=True, exist_ok=True)
    github.clone(repo, work, depth=None)
    empty = github.git(work, "rev-parse", "--verify", "--quiet", "HEAD", check=False).returncode != 0
    if empty:
        github.git(work, "symbolic-ref", "HEAD", f"refs/heads/{base}")
    else:
        github.git(work, "checkout", "--quiet", "-B", "sdlc/init", f"origin/{base}")
    created = scaffold(work, repo, base, jira_project, skeleton, package, ci, confluence_space, relay_to, registry_file)
    if not created:
        return {"repo": repo, "created": [], "message": "already set up"}
    github.git(work, "add", "-A")
    github.git(work, "commit", "--quiet", "-m", "chore: set up the SDLC pipeline (sdlc init)")
    target = base if empty else "sdlc/init"
    push = github.git(work, "push", "--quiet", *([] if empty else ["--force"]), "origin",
                      f"HEAD:refs/heads/{target}", check=False)
    if push.returncode != 0:
        why = push.stderr.strip()[-300:]
        if "workflow" in why:
            why += ("\nThe token needs the 'Workflows: read and write' permission (fine-grained) or the `workflow` "
                    "scope (classic) to add .github/workflows/sdlc.yml.")
        raise github.GitHubError(f"push to {target} failed: {why}")
    if empty:
        return {"repo": repo, "created": created, "pushed_to": base}
    pr = github.open_or_update_pr(repo, "sdlc/init", base, "chore: set up the SDLC pipeline",
                                  "Adds the SDLC pipeline's files:\n\n" + "\n".join(f"- `{c}`" for c in created)
                                  + "\n\nMerge it, then open an issue with the **Feature request (SDLC)** template.")
    return {"repo": repo, "created": created, "pr": pr["html_url"]}


def setup_github(repo: str, secrets: dict[str, str], variables: dict[str, str], allow_prs: bool) -> list[str]:
    """Labels, repo secrets (encrypted client-side), variables and the Actions PR permission."""
    done = [f"label {n}" for n, (color, desc) in LABELS.items() if github.ensure_label(repo, n, color, desc)]
    for name, value in secrets.items():
        github.set_secret(repo, name, value)
        done.append(f"secret {name}")
    for name, value in variables.items():
        github.set_variable(repo, name, value)
        done.append(f"variable {name}")
    if allow_prs:
        github.allow_actions_prs(repo)
        done.append("Actions may open PRs")
    return done


def project_snapshot(repo: str) -> Path:
    """A fresh shallow clone used only to read the repo's .sdlc/pipeline.yaml (the project config layer)."""
    snap = RUNS / "_cache" / repo.replace("/", "__")
    if snap.exists():
        shutil.rmtree(snap)
    snap.parent.mkdir(parents=True, exist_ok=True)
    github.clone(repo, snap, depth=1)
    return snap
