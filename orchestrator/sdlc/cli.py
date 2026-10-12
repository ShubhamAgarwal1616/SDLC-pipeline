"""The `sdlc` CLI (PLAN.md §21). Commands not built yet say which phase delivers them."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
import yaml
from rich.console import Console
from rich.prompt import Prompt
from rich.table import Table

from sdlc import answers
from sdlc.config import ConfigError, load_config, parse_overrides
from sdlc.engine import Engine, EngineError, RunState, RunStatus, RunStore
from sdlc.engine.runner import stages_of
from sdlc.stages import ARRIVES_IN, REAL_STAGES

app = typer.Typer(no_args_is_help=True, add_completion=False,
                  help="Agentic SDLC pipeline: requirements → specs → tickets → reviewed PRs.")
console = Console()

ProjectOpt = Annotated[Path, typer.Option("--project", help="Repo dir holding .sdlc/pipeline.yaml")]
SetOpt = Annotated[Optional[list[str]], typer.Option("--set", help="Override any config key: key=value")]
ModelOpt = Annotated[Optional[list[str]], typer.Option("--model", help="Per-stage model: stage=model")]
GateOpt = Annotated[Optional[list[str]], typer.Option("--gate", help="Optional gate: name=on|off")]
BudgetOpt = Annotated[Optional[float], typer.Option("--budget", help="Run budget in USD")]
FakeOpt = Annotated[bool, typer.Option("--fake", help="Use fake stages (no Docker, LLM or services)")]
FakeFailOpt = Annotated[Optional[list[str]], typer.Option("--fake-fail", help="Fake failures: stage=N")]
DryRunOpt = Annotated[bool, typer.Option("--dry-run", help="No external writes")]
RepoOpt = Annotated[Optional[str], typer.Option("--repo", help="GitHub repo owner/name (its .sdlc/pipeline.yaml is the project config)")]


def _project_dir(repo: str | None, project: Path) -> Path:
    if not repo:
        return project
    from sdlc.init_repo import project_snapshot
    return project_snapshot(repo)


def _fail(message: str) -> None:
    console.print(f"[red]error:[/] {message}")
    raise typer.Exit(1)


def _not_yet(command: str, phase: str) -> None:
    console.print(f"[yellow]`sdlc {command}` arrives in {phase}[/] (see BUILD_AND_ROLLOUT.md).")
    raise typer.Exit(1)


def _overrides(sets: list[str] | None, models: list[str] | None, gates: list[str] | None,
               budget: float | None) -> dict[str, Any]:
    out = parse_overrides(sets or [])
    for pair in models or []:
        stage, _, model = pair.partition("=")
        if not model:
            raise ConfigError(f"--model {pair!r} must look like stage=model")
        out[f"llm.stages.{stage}.model"] = model
    for pair in gates or []:
        name, _, value = pair.partition("=")
        if value not in {"on", "off"}:
            raise ConfigError(f"--gate {pair!r} must look like name=on|off")
        out[f"gates.{name}"] = value == "on"
    if budget is not None:
        out["budgets.per_run_usd"] = budget
    return out


def _fake_failures(pairs: list[str] | None) -> dict[str, int]:
    out = {}
    for pair in pairs or []:
        stage, _, n = pair.partition("=")
        if not n.isdigit():
            raise ConfigError(f"--fake-fail {pair!r} must look like stage=N")
        out[stage] = int(n)
    return out


def _auto_fakes(pipeline: str, fake: bool) -> list[str]:
    """Without --fake, run every real stage that exists and fake the rest (and say so)."""
    if fake:
        return []
    missing = [s for s in stages_of(pipeline) if s not in REAL_STAGES]
    if missing:
        console.print("[dim]Fake until built: " + ", ".join(f"{s} ({ARRIVES_IN.get(s, '?')})" for s in missing) + "[/]")
    return missing


def _engine() -> Engine:
    return Engine(RunStore())


def _show(state: RunState) -> None:
    """Print where the run is and what to do next."""
    rid = state.run_id
    colour = {"done": "green", "escalated": "red", "aborted": "red", "waiting_human": "yellow"}.get(
        state.status.value, "cyan")
    console.print(f"[bold]{rid}[/] · {state.pipeline} · [{colour}]{state.status.value}[/]"
                  + (" · fake" if state.fake else ""))
    if state.status == RunStatus.WAITING_HUMAN and state.wait:
        gate, info = state.wait.gate, state.wait.info
        console.print(f"Waiting at [bold]{gate}[/] since {state.wait.since}")
        if gate == "h0_answers":
            table = Table("id", "question", "default", "for", box=None)
            for q in info.get("questions", []):
                table.add_row(q["id"], q["text"], str(q.get("default")), q.get("audience", ""))
            console.print(table)
            path = RunStore().run_dir(rid) / "answers.md"
            if path.is_relative_to(Path.cwd()):
                path = path.relative_to(Path.cwd())
            console.print(f"Answer now:      [bold]sdlc answer {rid}[/]\n"
                          f"or fill in:      {path}\n"
                          f"then run:        sdlc answer {rid} --file {path}\n"
                          f"stop grilling:   sdlc answer {rid} --done")
        else:
            for key, value in info.items():
                console.print(f"  {key}: {value}")
            console.print(f"Next: [bold]sdlc review {rid} --approve[/]  |  --changes \"…\"  |  --abort")
    elif state.status == RunStatus.ESCALATED and state.escalation:
        console.print(f"[red]Escalated[/] at {state.escalation['at']}: {state.escalation['reason']}")
        console.print(f"Next: [bold]sdlc resume {rid}[/] [--from STAGE]  |  sdlc abort {rid}")
    elif state.status == RunStatus.DONE:
        console.print(f"Finished. Details: sdlc inspect {rid}   Costs: sdlc report {rid}")


# ---- Setup -----------------------------------------------------------------------------

CI_SECRETS = {"OPENROUTER_API_KEY": "OpenRouter API key", "JIRA_BASE_URL": "Jira base URL (https://you.atlassian.net)",
              "JIRA_USER_EMAIL": "Jira bot user email", "JIRA_API_TOKEN": "Jira API token"}
MODEL_VARS = ["SDLC_MODEL_DEFAULT", "SDLC_MODEL_GRILL", "SDLC_MODEL_PROPOSE", "SDLC_MODEL_IMPLEMENT",
              "SDLC_MODEL_REVIEW", "SDLC_MODEL_RESOLVE_COMMENTS", "SDLC_MODEL_REVIEW_FOLLOWUP", "SDLC_ALLOWED_MODELS", "SDLC_MODEL_MAP_JSON"]


def _github_token() -> str:
    """The person's own token for setting up the repo: .env / environment, `gh auth token`, or a prompt."""
    import os
    import shutil
    import subprocess
    from sdlc import secrets
    token = secrets.get("GITHUB_TOKEN", required=False)
    if not token and shutil.which("gh"):
        token = subprocess.run(["gh", "auth", "token"], capture_output=True, text=True).stdout.strip() or None
        if token:
            console.print("[dim]Using your GitHub CLI login (gh auth token).[/]")
    if not token:
        console.print("A GitHub token is needed to set up the repo: fine-grained, for this repo, with read/write on "
                      "Contents, Pull requests, Issues, Workflows, Secrets, Variables and Administration.")
        token = Prompt.ask("GitHub token", password=True).strip()
        if token and typer.confirm("Save it to ~/.sdlc/.env for next time?", default=True):
            secrets.save({"GITHUB_TOKEN": token})
    if not token:
        _fail("no GitHub token")
    os.environ["GITHUB_TOKEN"] = token   # this process only
    return token


@app.command()
def init(repo: Annotated[Optional[str], typer.Option("--repo", help="GitHub repo owner/name (default: this folder's origin)")] = None,
         jira_project: Annotated[Optional[str], typer.Option("--jira-project", help="Jira project key")] = None,
         skeleton: Annotated[Optional[str], typer.Option(help="Starter service for an empty repo: kotlin-http4k | none")] = None,
         package: Annotated[Optional[str], typer.Option(help="Base package for the skeleton, e.g. com.acme.orders")] = None,
         confluence_space: Annotated[Optional[str], typer.Option("--confluence-space", help="Mirror specs to this Confluence space")] = None,
         relay_to: Annotated[Optional[str], typer.Option("--relay-to", help="Member of multi-repo features run from this home repo (owner/name)")] = None,
         registry_file: Annotated[Optional[Path], typer.Option("--registry", exists=True, dir_okay=False, help="Feature home: repos.yaml listing the repos of multi-repo features")] = None,
         ci: Annotated[bool, typer.Option("--ci/--no-ci", help="Run the pipeline in GitHub Actions (workflow, issue form, secrets)")] = True,
         configure_secrets: Annotated[bool, typer.Option("--secrets/--no-secrets", help="Set the repo's Actions secrets")] = True,
         yes: Annotated[bool, typer.Option("--yes", "-y", help="Accept defaults; don't ask")] = False) -> None:
    """Set up a repo for the pipeline: config, OpenSpec, CLAUDE.md, the GitHub Actions workflow, the feature-request
    issue form, labels and secrets. Then open an issue with the "Feature request (SDLC)" template. No Docker needed."""
    import secrets as pysecrets
    from sdlc import secrets
    from sdlc.connectors import github
    from sdlc.init_repo import SKELETONS, init_repo, setup_github

    repo = repo or github.repo_from_remote(Path.cwd())
    if not repo:
        repo = Prompt.ask("GitHub repo (owner/name)")
    _github_token()
    try:
        info = github.repo_info(repo)
    except github.GitHubError as exc:
        _fail(f"can't read {repo}: {exc}")
    console.print(f"Setting up [bold]{repo}[/] (base branch {info.get('default_branch') or 'main'}).")
    if not jira_project:
        jira_project = (Prompt.ask("Jira project key (e.g. KAN)") if not yes else None) or None
    empty = info.get("size", 0) == 0
    if skeleton is None and empty and not yes:
        console.print("The repo is empty. The sandbox builds offline and never adds dependencies, so the AI needs a "
                      "buildable starting point with its libraries declared.")
        skeleton = Prompt.ask("Starter service", choices=[*SKELETONS, "none"], default=SKELETONS[0])
    skeleton = None if skeleton in (None, "none") else skeleton

    secret_values: dict[str, str] = {}
    variables: dict[str, str] = {}
    allow_prs = False
    if relay_to and ci and configure_secrets:   # a member repo: it only relays PR events to the home repo
        try:
            have = "SDLC_RELAY_TOKEN" in github.secret_names(repo)
        except github.GitHubError as exc:
            _fail(f"can't list {repo}'s Actions secrets ({exc}); re-run with --no-secrets and set SDLC_RELAY_TOKEN yourself")
        if not have:
            value = secrets.get("SDLC_RELAY_TOKEN", required=False) or (None if yes else Prompt.ask(
                f"Token that may dispatch to {relay_to} (Contents: read and write there)", password=True).strip())
            if not value:
                _fail("SDLC_RELAY_TOKEN is needed for --relay-to")
            secret_values["SDLC_RELAY_TOKEN"] = value
    elif ci and configure_secrets:
        try:
            existing = github.secret_names(repo)
        except github.GitHubError as exc:
            _fail(f"can't list {repo}'s Actions secrets ({exc}). The token needs 'Secrets: read and write'; "
                  "or re-run with --no-secrets and set them yourself.")
        to_save: dict[str, str] = {}
        for name, label in CI_SECRETS.items():
            if name in existing:
                console.print(f"[dim]{name}: already set on the repo[/]")
                continue
            value = secrets.get(name, required=False)
            if value:
                console.print(f"[dim]{name}: from your saved settings[/]")
            elif yes:
                _fail(f"{name} is missing (set it in ~/.sdlc/.env or run without --yes)")
            else:
                value = Prompt.ask(label, password=name != "JIRA_BASE_URL" and name != "JIRA_USER_EMAIL").strip()
                to_save[name] = value
            secret_values[name] = value
        if confluence_space and "CONFLUENCE_BASE_URL" not in existing:
            value = secrets.get("CONFLUENCE_BASE_URL", required=False) or (
                None if yes else Prompt.ask("Confluence base URL (Enter: <Jira URL>/wiki)", default="",
                                            show_default=False).strip())
            if value:
                secret_values["CONFLUENCE_BASE_URL"] = value
        if "SDLC_STATE_KEY" not in existing:   # encrypts the run state the workflow keeps between jobs
            secret_values["SDLC_STATE_KEY"] = pysecrets.token_urlsafe(32)
        if "SDLC_GITHUB_TOKEN" not in existing and registry_file:
            bot = secrets.get("SDLC_GITHUB_TOKEN", required=False) or (None if yes else Prompt.ask(
                "Bot token for the feature's repos (SDLC_GITHUB_TOKEN: Contents, Pull requests and Issues read/write "
                "on every repo in the registry)", password=True).strip())
            if not bot:
                _fail("a feature home (--registry) needs SDLC_GITHUB_TOKEN")
            secret_values["SDLC_GITHUB_TOKEN"] = bot
        elif "SDLC_GITHUB_TOKEN" not in existing and not yes:
            console.print("Optional: a token for the bot (secret SDLC_GITHUB_TOKEN). With it, the bot's PRs trigger "
                          "your CI checks; without it the workflow's built-in token is used and they don't.")
            bot = Prompt.ask("Bot token (Enter to skip)", password=True, default="", show_default=False).strip()
            if bot:
                secret_values["SDLC_GITHUB_TOKEN"] = bot
        if "SDLC_GITHUB_TOKEN" not in existing and "SDLC_GITHUB_TOKEN" not in secret_values:
            allow_prs = yes or typer.confirm("Allow GitHub Actions to open PRs in this repo (needed without a bot token)?",
                                             default=True)
        variables = {n: v for n in MODEL_VARS if (v := secrets.get(n, required=False))}
        if to_save and not yes and typer.confirm("Save the values you typed to ~/.sdlc/.env for other repos?",
                                                 default=True):
            console.print(f"[dim]Saved to {secrets.save(to_save)} (mode 600).[/]")
    try:
        if ci:
            for line in setup_github(repo, secret_values, variables, allow_prs):
                console.print(f"[green]✓[/] {line}")
        result = init_repo(repo, jira_project, skeleton=skeleton, package=package, ci=ci,
                           confluence_space=confluence_space, relay_to=relay_to, registry_file=registry_file)
    except Exception as exc:  # connectors raise their own error types
        _fail(str(exc))
    for line in result.get("created") or [result.get("message", "")]:
        console.print(f"[green]✓[/] {line}")
    if result.get("pushed_to"):
        console.print(f"Pushed the first commit to [bold]{result['pushed_to']}[/].")
    if result.get("pr"):
        console.print(f"Opened [bold]{result['pr']}[/]: merge it to finish the setup.")
    if ci and relay_to:
        console.print(f"\n[bold]Done:[/] PR events here now wake the pipeline in {relay_to}. Features are requested "
                      f"there (list this repo in its .sdlc/repos.yaml).")
    elif ci:
        console.print(f"\n[bold]Next:[/] open an issue at https://github.com/{repo}/issues/new/choose with the "
                      "[bold]Feature request (SDLC)[/] template. The pipeline asks its questions in the issue.")
        console.print("Recommended: protect the base branch (Settings → Branches) so only reviewed PRs reach it.")


@app.command()
def doctor(repo: RepoOpt = None) -> None:
    """Check this machine (Docker, images, networks, gateway, .env) and, with --repo, the repo (access,
    protection, .sdlc config, openspec, CLAUDE.md size, skills, Jira project). Read-only."""
    from sdlc.doctor import run_doctor
    checks = run_doctor(repo)
    table = Table("check", "status", "detail")
    colour = {"pass": "green", "warn": "yellow", "fail": "red"}
    for c in checks:
        table.add_row(c.name, f"[{colour[c.status]}]{c.status}[/]", c.detail)
    console.print(table)
    failed = [c for c in checks if c.status == "fail"]
    console.print(f"{len(checks) - len(failed)}/{len(checks)} ok" + (f", {len(failed)} failing" if failed else ""))
    if failed:
        raise typer.Exit(1)


@app.command("config")
def config_cmd(project: ProjectOpt = Path("."), set_: SetOpt = None, model: ModelOpt = None,
               gate: GateOpt = None, budget: BudgetOpt = None,
               explain: Annotated[Optional[str], typer.Option(help="Show where KEY's value comes from")] = None,
               as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Show the effective config and which layer set each value."""
    try:
        cfg = load_config(project, _overrides(set_, model, gate, budget))
        if explain:
            table = Table("key", "value", "set by")
            for key, value, layer in cfg.explain(explain):
                table.add_row(key, json.dumps(value), layer)
            console.print(table)
        elif as_json:
            console.print_json(cfg.to_json())
        else:
            console.print(yaml.safe_dump(cfg.data, sort_keys=False))
    except ConfigError as exc:
        _fail(str(exc))


skills_app = typer.Typer(no_args_is_help=True, help="Inspect, lint and add skills (org library + repo skills).")
app.add_typer(skills_app, name="skills")


def _skill_roots(repo: str | None) -> list[tuple[str, Path, bool]]:
    from sdlc.config import ROOT
    roots = [("org", ROOT / "skills", False)]
    if repo:
        from sdlc.init_repo import project_snapshot
        roots.append(("repo", project_snapshot(repo) / ".claude" / "skills", True))
    return roots


@skills_app.command("list")
def skills_list(repo: RepoOpt = None, stage: Annotated[Optional[str], typer.Option(help="Show what a stage loads")] = None) -> None:
    """List skills by layer: entry skills (invoked by name) and auto-picked ones (by description)."""
    from sdlc.skills_lint import list_skills
    cfg = load_config()
    entry = {k: v.get("entry_skill") or v.get("entry_skills") for k, v in (cfg.get("stages") or {}).items()}
    table = Table("layer", "skill", "kind", "entry for stage", "description")
    for layer, root, _ in _skill_roots(repo):
        for s in list_skills(root):
            stages = [k for k, v in entry.items() if s.name == v or (isinstance(v, list) and s.name in v)]
            if stage and stage not in stages and s.entry_only:
                continue
            table.add_row(layer, s.name, "entry (by name)" if s.entry_only else "auto-picked", ", ".join(stages),
                          s.description[:90])
    console.print(table)


@skills_app.command("lint")
def skills_lint_cmd(repo: RepoOpt = None) -> None:
    """Check frontmatter, descriptions (do they say WHEN?), reserved names, references and size."""
    from sdlc.skills_lint import lint_dir, list_skills
    known: set[str] = set()
    for _, root, _ in _skill_roots(repo):
        known |= {s.name for s in list_skills(root)}
    errors = 0
    for layer, root, repo_level in _skill_roots(repo):
        for f in lint_dir(root, repo_level, known):
            errors += f.level == "error"
            console.print(f"[{'red' if f.level == 'error' else 'yellow'}]{f.level}[/] {layer}/{f.skill}: {f.message}")
    console.print("clean" if not errors else f"{errors} error(s)")
    if errors:
        raise typer.Exit(1)


@skills_app.command("add")
def skills_add(path: Annotated[Path, typer.Argument(exists=True, file_okay=False)], repo: RepoOpt = None) -> None:
    """Add a skill folder to a repo's .claude/skills via a PR (lints it first)."""
    import shutil
    from sdlc.config import RUNS
    from sdlc.connectors import github
    from sdlc.skills_lint import lint_dir, parse_skill
    if not repo:
        _fail("--repo owner/name is required")
    if not (path / "SKILL.md").exists():
        _fail(f"{path} has no SKILL.md")
    meta, _ = parse_skill(path / "SKILL.md")
    name = str(meta.get("name") or path.name)
    staging = RUNS / "_init" / "skill-lint" / name
    shutil.rmtree(staging.parent, ignore_errors=True)
    shutil.copytree(path, staging)
    problems = [f for f in lint_dir(staging.parent, repo_level=True) if f.level == "error"]
    if problems:
        _fail("; ".join(f.message for f in problems))
    work = RUNS / "_init" / f"skill-{name}"
    shutil.rmtree(work, ignore_errors=True)
    github.clone(repo, work, depth=None)
    base = github.repo_info(repo)["default_branch"]
    github.git(work, "checkout", "--quiet", "-B", f"skills/add-{name}", f"origin/{base}")
    shutil.copytree(path, work / ".claude" / "skills" / name, dirs_exist_ok=True)
    github.git(work, "add", "-A")
    github.git(work, "commit", "--quiet", "-m", f"chore(skills): add {name}")
    github.git(work, "push", "--quiet", "--force", "origin", f"HEAD:refs/heads/skills/add-{name}")
    pr = github.open_or_update_pr(repo, f"skills/add-{name}", base, f"chore(skills): add {name}",
                                  f"Adds the `{name}` skill to `.claude/skills/`.\n\n> {meta.get('description', '')}")
    console.print(f"Opened {pr['html_url']} — merge it and the next run picks the skill up.")


# ---- Pipeline A ------------------------------------------------------------------------

@app.command()
def plan(requirements: Annotated[Path, typer.Argument(exists=True, dir_okay=False)],
         context: Annotated[Optional[Path], typer.Option(exists=True, dir_okay=False)] = None,
         project: ProjectOpt = Path("."), repo: RepoOpt = None,
         repos: Annotated[Optional[str], typer.Option("--repos", help="Multi-repo feature: registry names, e.g. notes-api,notes-client")] = None,
         set_: SetOpt = None, model: ModelOpt = None,
         gate: GateOpt = None, budget: BudgetOpt = None, fake: FakeOpt = False,
         fake_fail: FakeFailOpt = None, dry_run: DryRunOpt = False,
         run: Annotated[Optional[str], typer.Option("--run", help="With --update: the plan run to update")] = None,
         update: Annotated[bool, typer.Option("--update", help="Replace RUN's requirement with this file")] = False) -> None:
    """Start Pipeline A on a requirements file (one repo with --repo, or a feature across repos with --repos).
    `--run RUN --update` replaces a running plan's requirement: the grill asks only about what changed."""
    if update or run:
        if not (update and run):
            _fail("use --run RUN and --update together")
        try:
            _show(_engine().update_requirements(run, requirements.resolve()))
        except (KeyError, EngineError) as exc:
            _fail(str(exc))
        return
    try:
        names = [n.strip() for n in repos.split(",") if n.strip()] if repos else None
        if names:
            from sdlc import registry
            reg = registry.load()
            unknown = [n for n in names if n not in reg]
            if unknown:
                raise ConfigError(f"unknown repos {unknown}; registered: {sorted(reg)}")
            repo = repo or reg[registry.ordered(names, reg)[0]]["github"]   # project config from the first provider
        cfg = load_config(_project_dir(repo, project), _overrides(set_, model, gate, budget))
        inputs = {"requirements": str(requirements.resolve()), "context": str(context.resolve()) if context else None,
                  "project": str(project.resolve()), "repo": repo, "repos": names, "dry_run": dry_run}
        state = _engine().start("plan", cfg, inputs, fake=fake, fake_failures=_fake_failures(fake_fail),
                                fake_stages=_auto_fakes("plan", fake))
        _show(state)
    except (ConfigError, EngineError) as exc:
        _fail(str(exc))


@app.command()
def answer(run_id: str,
           file: Annotated[Optional[Path], typer.Option("--file", exists=True, dir_okay=False)] = None,
           accept_defaults: Annotated[bool, typer.Option("--accept-defaults")] = False,
           done: Annotated[bool, typer.Option("--done", help="Stop grilling; open items become assumptions")] = False
           ) -> None:
    """Answer pending grill questions (H0)."""
    engine = _engine()
    try:
        state = engine.store.load(run_id)
        if state.status != RunStatus.WAITING_HUMAN or not state.wait or state.wait.gate != "h0_answers":
            _fail(f"{run_id} isn't waiting for grill answers ({state.status.value})")
        questions = state.wait.info.get("questions", [])
        if done:
            state = engine.deliver(run_id, "done", {"answers": answers.defaults(questions), "channel": "cli"})
        else:
            if file:
                result, channel = answers.parse(file.read_text(), questions), "file"
            elif accept_defaults:
                result, channel = answers.defaults(questions), "cli"
            else:
                console.print("Press Enter to accept the default. Type [bold]skip[/] if nobody knows yet.")
                replies = {}
                for q in questions:
                    default = str(q.get("default") or "")
                    reply = Prompt.ask(f"[bold]{q['id']}[/] {q['text']}", default=default)
                    replies[q["id"]] = "" if reply == default else reply  # unchanged → source "default"
                result, channel = answers.from_values(questions, replies), "cli"
            state = engine.deliver(run_id, "answered", {"answers": result, "channel": channel})
        _show(state)
    except (KeyError, EngineError) as exc:
        _fail(str(exc))


@app.command()
def review(run_id: str,
           approve: Annotated[bool, typer.Option("--approve")] = False,
           changes: Annotated[Optional[str], typer.Option("--changes", help="Requested changes (free text)")] = None,
           abort_: Annotated[bool, typer.Option("--abort")] = False) -> None:
    """Approve, request changes on, or abort at a review gate (H1, H2, H3)."""
    engine = _engine()
    try:
        state = engine.store.load(run_id)
        if state.status != RunStatus.WAITING_HUMAN or not state.wait or state.wait.gate == "h0_answers":
            _fail(f"{run_id} isn't waiting at a review gate ({state.status.value})")
        if sum([approve, changes is not None, abort_]) != 1:
            _show(state)
            choice = Prompt.ask("Decision", choices=["approve", "changes", "abort"])
            approve, abort_ = choice == "approve", choice == "abort"
            if choice == "changes":
                changes = Prompt.ask("What should change?")
        if approve:
            state = engine.deliver(run_id, "approved")
        elif abort_:
            state = engine.deliver(run_id, "abort")
        else:
            state = engine.deliver(run_id, "changes", {"text": changes})
        _show(state)
    except (KeyError, EngineError) as exc:
        _fail(str(exc))


# ---- Pipeline B ------------------------------------------------------------------------

@app.command()
def implement(key: Annotated[Optional[str], typer.Argument(help="Jira story key")] = None,
              feature: Annotated[Optional[str], typer.Option(help="Multi-repo feature run")] = None,
              change_id: Annotated[Optional[str], typer.Option("--change-id")] = None,
              project: ProjectOpt = Path("."), repo: RepoOpt = None, set_: SetOpt = None, model: ModelOpt = None,
              gate: GateOpt = None, budget: BudgetOpt = None, fake: FakeOpt = False,
              fake_fail: FakeFailOpt = None, dry_run: DryRunOpt = False) -> None:
    """Start Pipeline B for one story."""
    if feature:
        from sdlc import feature as features
        try:
            for line in features.start(_engine(), feature):
                console.print(line)
            _show_feature(feature)
        except Exception as exc:
            _fail(str(exc))
        return
    if not key:
        _fail("give a Jira key, e.g. sdlc implement TODO-42")
    try:
        cfg = load_config(_project_dir(repo, project), _overrides(set_, model, gate, budget))
        inputs = {"ticket": key, "change_id": change_id, "project": str(project.resolve()), "repo": repo,
                  "dry_run": dry_run}
        if not change_id:
            inputs.pop("change_id")
        state = _engine().start("implement", cfg, inputs, fake=fake, fake_failures=_fake_failures(fake_fail),
                                fake_stages=_auto_fakes("implement", fake))
        _show(state)
    except (ConfigError, EngineError) as exc:
        _fail(str(exc))


# ---- Run control -----------------------------------------------------------------------

@app.command()
def status(run_id: Annotated[Optional[str], typer.Argument()] = None,
           waiting: Annotated[bool, typer.Option("--waiting")] = False) -> None:
    """List runs, or show one run."""
    store = RunStore()
    try:
        if run_id:
            state = store.load(run_id)
            _show(state)
            if state.loops:
                console.print("Loops used: " + ", ".join(f"{k}={v}" for k, v in state.loops.items()))
            events = store.run_dir(run_id) / "events.jsonl"
            if events.exists():
                for line in events.read_text().splitlines():
                    e = json.loads(line)
                    if e.get("kind") == "budget_warning":
                        console.print(f"[yellow]budget warning[/]: {e['scope']} {e['stage']} at "
                                      f"${e['spent']:.2f} of ${e['budget']}")
            return
    except KeyError as exc:
        _fail(str(exc))
    table = Table("run", "pipeline", "status", "at", "updated", "fake")
    for run in store.list():
        if waiting and run.status != RunStatus.WAITING_HUMAN:
            continue
        table.add_row(run.run_id, run.pipeline, run.status.value, run.current or "-", run.updated,
                      "yes" if run.fake else "")
    console.print(table)


LAUNCHD_LABEL = "com.sdlc-pipeline.watch"


def _write_launchd(interval: int) -> Path:
    import shutil
    from sdlc.config import HOME, INSTALLED
    uv = shutil.which("uv") or "uv"
    sdlc = shutil.which("sdlc") or "sdlc"
    program = [sdlc] if INSTALLED else [uv, "run", "sdlc"]
    plist = HOME / "launchd" / f"{LAUNCHD_LABEL}.plist"
    plist.parent.mkdir(parents=True, exist_ok=True)
    plist.write_text(f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>{LAUNCHD_LABEL}</string>
  <key>ProgramArguments</key><array>
    {"".join(f"<string>{p}</string>" for p in program)}<string>watch</string>
    <string>--interval</string><string>{interval}</string></array>
  <key>WorkingDirectory</key><string>{HOME}</string>
  <key>EnvironmentVariables</key><dict><key>PATH</key><string>/usr/local/bin:/opt/homebrew/bin:/usr/bin:/bin</string></dict>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>{HOME}/runs/_watch.log</string>
  <key>StandardErrorPath</key><string>{HOME}/runs/_watch.log</string>
</dict></plist>
""")
    return plist


feature_app = typer.Typer(no_args_is_help=True, help="Multi-repo feature runs (one Epic, stories across repos).")
app.add_typer(feature_app, name="feature")


def _show_feature(epic: str) -> None:
    from sdlc import feature as features
    state = features.load(epic)
    console.print(f"[bold]feature {epic}[/] · {state['status']}" + (f" · {state['note']}" if state.get("note") else ""))
    table = Table("story", "repo", "blocked by", "jira", "run")
    store = RunStore()
    for st in state["stories"]:
        run = f"{st['run_id']} [{store.load(st['run_id']).status.value}]" if st["run_id"] else "-"
        table.add_row(st["key"], st.get("repo") or "?", ", ".join(st["blocked_by"]) or "-", st["jira_status"], run)
    console.print(table)


@feature_app.command("status")
def feature_status(epic: str) -> None:
    """Show a feature's stories, their order, Jira status and child runs."""
    try:
        _show_feature(epic)
    except FileNotFoundError:
        _fail(f"no feature run for {epic}: start one with `sdlc implement --feature {epic}`")


@feature_app.command("advance")
def feature_advance(epic: str, dry_run: Annotated[bool, typer.Option("--dry-run", help="Only say what's ready")] = False) -> None:
    """Start every story whose blockers are Done (sdlc watch does this automatically)."""
    from sdlc import feature as features
    for line in features.advance(_engine(), epic, start_children=not dry_run):
        console.print(line)
    _show_feature(epic)


@app.command()
def watch(interval: Annotated[int, typer.Option(help="Seconds between polls")] = 60,
          once: Annotated[bool, typer.Option("--once", help="Poll once and exit")] = False,
          launchd: Annotated[bool, typer.Option("--launchd", help="Write a macOS LaunchAgent for a background watcher (you load it)")] = False) -> None:
    """Poll GitHub for /sdlc commands and merges on waiting PR gates; send reminders; resume those runs."""
    import time
    from sdlc.watch import poll_once
    if launchd:
        plist = _write_launchd(interval)
        target = f"~/Library/LaunchAgents/{LAUNCHD_LABEL}.plist"
        console.print(f"Wrote {plist}\nTo run `sdlc watch` in the background (and at login):\n"
                      f"  cp {plist} {target}\n  launchctl load {target}\n"
                      f"Logs: runs/_watch.log · Stop: launchctl unload {target}")
        return
    engine = _engine()
    while True:
        try:
            actions = poll_once(engine)
        except Exception as exc:  # keep watching through transient API errors
            actions = [f"[red]poll failed:[/] {exc}"]
        for line in actions:
            console.print(line)
        if once:
            if not actions:
                console.print("Nothing to do.")
            return
        time.sleep(interval)


@app.command()
def resume(run_id: str,
           from_: Annotated[Optional[str], typer.Option("--from", help="Restart from this stage")] = None,
           set_: SetOpt = None, budget: BudgetOpt = None) -> None:
    """Continue an escalated or interrupted run, optionally raising budgets or limits (--set)."""
    try:
        _show(_engine().resume(run_id, from_, _overrides(set_, None, None, budget)))
    except (KeyError, EngineError, ConfigError) as exc:
        _fail(str(exc))


@app.command()
def abort(run_id: str, reason: Annotated[str, typer.Option()] = "") -> None:
    """Stop a run."""
    try:
        _show(_engine().abort(run_id, reason))
    except (KeyError, EngineError) as exc:
        _fail(str(exc))


# ---- Insight ---------------------------------------------------------------------------

@app.command()
def report(run_id: Annotated[Optional[str], typer.Argument()] = None,
           all_runs: Annotated[bool, typer.Option("--all", help="Across all runs: by run, stage and model")] = False,
           as_json: Annotated[bool, typer.Option("--json")] = False) -> None:
    """Per-stage tokens, cost, model and retries for a run, or (--all) totals across runs."""
    if all_runs or not run_id:
        from sdlc.reporting import collect
        data = collect()
        if as_json:
            console.print_json(json.dumps(data))
            return
        t = data["totals"]
        console.print(f"[bold]${t['cost']:.2f}[/] over {t['calls']} LLM calls · {t['runs']} runs ({t['done']} done) · "
                      f"cache hit {t['cache_hit'] * 100:.0f}% · gateway errors {t['errors']}")
        runs = Table("run", "pipeline", "subject", "status", "$", "calls", "loops", "escal.", "min")
        for r in data["runs"]:
            runs.add_row(r["run_id"], r["pipeline"], str(r["subject"]), r["status"], f"{r['cost']:.2f}",
                         str(r["calls"]), str(r["loops"]), str(r["escalations"]), str(r["elapsed_min"]))
        console.print(runs)
        stages = Table("stage", "calls", "$", "cache hit")
        for name, v in sorted(data["stages"].items(), key=lambda kv: -kv[1]["cost"]):
            stages.add_row(name, str(v["calls"]), f"{v['cost']:.2f}", f"{v['cache_hit'] * 100:.0f}%")
        console.print(stages)
        models = Table("model", "calls", "$")
        for name, v in sorted(data["models"].items(), key=lambda kv: -kv[1]["cost"]):
            models.add_row(name, str(v["calls"]), f"{v['cost']:.2f}")
        console.print(models)
        return
    try:
        state = RunStore().load(run_id)
    except KeyError as exc:
        _fail(str(exc))
    rows = [{"stage": h.stage, "attempt": h.attempt, "outcome": h.outcome,
             "model": ",".join(m.replace("anthropic/", "") for m in h.usage.get("models", []) or []) or "-",
             "input_tokens": h.usage.get("input_tokens", 0), "output_tokens": h.usage.get("output_tokens", 0),
             "cache_read_tokens": h.usage.get("cache_read_tokens", 0), "cost_usd": h.usage.get("cost_usd", 0.0)}
            for h in state.history]
    if as_json:
        console.print_json(json.dumps(rows))
        return
    table = Table("stage", "try", "outcome", "model", "in", "out", "cache read", "cost $")
    for r in rows:
        table.add_row(r["stage"], str(r["attempt"]), r["outcome"], r["model"], str(r["input_tokens"]),
                      str(r["output_tokens"]), str(r["cache_read_tokens"]), f"{r['cost_usd']:.4f}")
    console.print(table)
    console.print(f"Total cost: ${sum(r['cost_usd'] for r in rows):.4f}"
                  + ("  (fake run: no LLM calls)" if state.fake else ""))


@app.command()
def dashboard(out: Annotated[Optional[Path], typer.Option(help="Output HTML path")] = None) -> None:
    """Write a static cost/runs dashboard (HTML) from run state and the gateway usage log."""
    from sdlc.config import RUNS
    from sdlc.reporting import write_dashboard
    path = write_dashboard(out or RUNS / "_dashboard" / "index.html")
    console.print(f"Wrote {path}  (open it in a browser)")


@app.command()
def inspect(run_id: str, stage: Annotated[Optional[str], typer.Option()] = None) -> None:
    """A run's history and artifacts, for debugging."""
    store = RunStore()
    try:
        state = store.load(run_id)
    except KeyError as exc:
        _fail(str(exc))
    console.print(f"Run dir: {store.run_dir(run_id)}")
    table = Table("#", "stage", "try", "outcome", "note", "artifacts")
    for i, h in enumerate(state.history, 1):
        if stage and h.stage != stage:
            continue
        table.add_row(str(i), h.stage, str(h.attempt), h.outcome, h.note or "", ", ".join(h.artifacts))
    console.print(table)
    if stage:
        for h in (h for h in state.history if h.stage == stage):
            for name, path in h.artifacts.items():
                text = Path(path).read_text() if Path(path).exists() else "(missing)"
                console.rule(f"{stage} try {h.attempt} · {name}")
                console.print(text[:2000], markup=False)


# ---- Maintainers ------------------------------------------------------------------------

@app.command("eval")
def eval_cmd(suite: Annotated[str, typer.Option(help="skills | redteam | all")] = "all",
             case: Annotated[Optional[str], typer.Option(help="Run one case by name")] = None,
             model: Annotated[Optional[str], typer.Option(help="Override SDLC_TEST_MODEL")] = None,
             list_only: Annotated[bool, typer.Option("--list", help="List cases without running")] = False) -> None:
    """Skill evals and red-team cases: one real sandbox turn each (on SDLC_TEST_MODEL), then checks."""
    from sdlc import evals
    cases = evals.discover(suite, case)
    if list_only or not cases:
        for p in cases:
            console.print(f"{p.parent.parent.name}/{p.parent.name}")
        if not cases:
            _fail(f"no cases match suite={suite} case={case}")
        return
    use = model or evals.test_model()
    console.print(f"Running {len(cases)} case(s) on [bold]{use}[/] …")
    results = evals.run_suite(suite, case, use)
    table = Table("case", "result", "status", "$", "s", "failed checks")
    for r in results:
        bad = "; ".join(f"{n} ({d})" for n, ok, d in r.checks if not ok)
        table.add_row(f"{r.suite}/{r.name}", "[green]pass[/]" if r.passed else "[red]FAIL[/]", r.status,
                      f"{r.cost:.3f}", f"{r.seconds:.0f}", bad[:160])
    console.print(table)
    console.print(f"{sum(r.passed for r in results)}/{len(results)} passed · ${sum(r.cost for r in results):.3f}")
    if not all(r.passed for r in results):
        raise typer.Exit(1)


# ---- Local infrastructure and CI mode --------------------------------------------------------------

@app.command()
def up(recreate: Annotated[bool, typer.Option(help="Restart the gateway even if one is running")] = False) -> None:
    """Pull the images, create the networks and start the LLM gateway (no docker compose or builds needed)."""
    from sdlc import infra
    cfg = load_config()
    try:
        infra.ensure(cfg, need_gateway=False)
        state = infra.gateway_up(cfg, recreate=recreate)
    except Exception as exc:
        _fail(str(exc))
    console.print(f"[green]ready[/]: worker {infra.images(cfg)['worker']}, gateway {state}")


@app.command()
def down() -> None:
    """Stop the LLM gateway container."""
    from sdlc import infra
    infra.down()
    console.print("gateway stopped")


@app.command("version")
def version_cmd() -> None:
    """Print the CLI version (images and the repo workflow use the same one)."""
    from sdlc.config import INSTALLED, version
    console.print(f"sdlc-pipeline {version()}" + ("" if INSTALLED else " (source checkout)"))


ci_app = typer.Typer(no_args_is_help=True, help="CI mode: the pipeline inside a repo's GitHub Actions.")
app.add_typer(ci_app, name="ci")


@ci_app.command("reconcile")
def ci_reconcile(repo: Annotated[Optional[str], typer.Option(help="owner/name (default: $GITHUB_REPOSITORY)")] = None,
                 state: Annotated[bool, typer.Option("--state/--no-state",
                                                     help="Restore and save the encrypted run state")] = True) -> None:
    """Start runs for new `sdlc` issues, apply /sdlc commands from issues and PRs, advance merged PRs and post
    updates. Idempotent: the workflow runs it on every trigger."""
    import os
    from sdlc import ci
    repo = repo or os.environ.get("GITHUB_REPOSITORY")
    if not repo:
        _fail("--repo owner/name (or GITHUB_REPOSITORY) is required")
    actions: list[str] = []
    try:
        if state:
            console.print(ci.restore(repo))
        actions = ci.reconcile(repo)
    except Exception as exc:
        actions.append(f"reconcile failed: {type(exc).__name__}: {exc}")
        raise
    finally:
        for line in actions:
            console.print(f"- {line}")
        ci.summary(actions)
        if state and (saved := ci.save()):
            console.print(f"state saved to {saved}")


if __name__ == "__main__":
    app()
