"""CI mode (sdlc ci reconcile), sdlc init's scaffold, and state persistence. No live services: GitHub is stubbed,
stages are the fakes."""

import io
import json
import tarfile
from pathlib import Path

import pytest

from sdlc import ci, infra, init_repo
from sdlc.engine import Engine, RunStatus, RunStore
from sdlc.stages import get_stage


@pytest.fixture
def home(tmp_path, monkeypatch):
    runs = tmp_path / "home" / "runs"
    monkeypatch.setattr(ci, "HOME", tmp_path / "home")
    monkeypatch.setattr(ci, "RUNS", runs)
    monkeypatch.setattr(ci, "CI_FILE", runs / "_ci" / "ci.json")
    monkeypatch.setattr(ci, "OUT", tmp_path / "home" / "out" / "state.enc")
    monkeypatch.setattr("sdlc.secrets.get", lambda name, required=True: {"SDLC_STATE_KEY": "k" * 43}.get(name))
    monkeypatch.setenv("SDLC_GATEWAY_DATA", str(tmp_path / "gw"))
    monkeypatch.setattr("sdlc.registry.REGISTRY", tmp_path / "home" / "config" / "repos.yaml")   # never the real one
    return runs


class FakeGitHub:
    """Just enough of the GitHub connector for a reconcile."""

    def __init__(self):
        self.issues = []
        self.comments: dict[int, list[dict]] = {}
        self.posted: list[tuple[int, str]] = []

    def install(self, monkeypatch, tmp_path):
        from sdlc.connectors import github
        monkeypatch.setattr(github, "requirement_issues", lambda repo, label="sdlc": self.issues)
        monkeypatch.setattr(github, "issue_comments", lambda repo, n: self.comments.get(n, []))
        monkeypatch.setattr(github, "comment", lambda repo, n, text: self.posted.append((n, text)) or {})
        monkeypatch.setattr("sdlc.init_repo.project_snapshot", lambda repo: tmp_path)

    def say(self, n, cid, body, association="OWNER"):
        self.comments.setdefault(n, []).append({"id": f"ic-{cid}", "author": "alice", "association": association,
                                                "at": "2099-01-01T00:00:00Z", "body": body})


@pytest.fixture
def engine(home):
    return Engine(RunStore(home), stage_lookup=lambda name, fake=False: get_stage(name, fake=True))


ISSUE = {"number": 7, "title": "[Feature] Rate limiting", "author_association": "OWNER",
         "body": "### Problem\n\nAPI abused\n\n### Users\n\nAPI clients\n\n### Goals\n\nFair use\n\n### Context\n\n_No response_"}


# ---- requirement text, answers, notices ----------------------------------------------------------------

def test_requirement_text_from_issue_form():
    text = ci.requirement_text(ISSUE)
    assert text.startswith("# Rate limiting\n")
    assert "### Problem" in text and "_No response_" not in text


def test_parse_answers_single_and_per_question():
    one = [{"id": "r1-q1", "default": "d"}]
    assert ci.parse_answers("use Redis", one)[0] == {"id": "r1-q1", "value": "use Redis", "source": "human"}
    two = [{"id": "r1-q1", "default": "a"}, {"id": "r1-q2", "default": "b"}]
    got = ci.parse_answers("r1-q2: 30 seconds", two)
    assert got[0]["source"] == "default" and got[1]["value"] == "30 seconds"
    assert ci.parse_answers("free text", two) is None


# ---- reconcile -------------------------------------------------------------------------------------------

def test_reconcile_runs_the_issue_through_questions_answers_and_spec(engine, monkeypatch, tmp_path):
    gh = FakeGitHub()
    gh.install(monkeypatch, tmp_path)
    gh.issues = [ISSUE]

    ci.reconcile("o/r", engine)
    plan = ci.runs_for_issue(engine.store, 7, "plan")[0]
    assert plan.status == RunStatus.WAITING_HUMAN and plan.wait.gate == "h0_answers"
    assert plan.inputs["issue"] == 7 and Path(plan.inputs["requirements"]).read_text().startswith("# Rate limiting")
    assert any("### Questions" in text for _, text in gh.posted)

    ci.reconcile("o/r", engine)            # idempotent: no second run, no repeated questions
    assert len(ci.runs_for_issue(engine.store, 7, "plan")) == 1
    assert sum("### Questions" in t for _, t in gh.posted) == 1

    gh.say(7, 1, "/sdlc accept")
    ci.reconcile("o/r", engine)
    gh.say(7, 2, "/sdlc done")
    ci.reconcile("o/r", engine)
    plan = engine.store.load(plan.run_id)
    assert plan.wait and plan.wait.gate == "h1_spec_review"
    assert any("Spec ready for review" in t for _, t in gh.posted)


def test_untrusted_issue_and_comments_are_ignored(engine, monkeypatch, tmp_path):
    gh = FakeGitHub()
    gh.install(monkeypatch, tmp_path)
    gh.issues = [{**ISSUE, "author_association": "NONE"}]
    ci.reconcile("o/r", engine)
    ci.reconcile("o/r", engine)
    assert not engine.store.list()
    assert sum("Only repo collaborators" in t for _, t in gh.posted) == 1


def test_orphaned_running_run_escalates_for_resume(engine, monkeypatch, tmp_path):
    gh = FakeGitHub()
    gh.install(monkeypatch, tmp_path)
    gh.issues = [ISSUE]
    ci.reconcile("o/r", engine)
    plan = ci.runs_for_issue(engine.store, 7, "plan")[0]
    plan.status, plan.wait, plan.current = RunStatus.RUNNING, None, "a2_propose"
    engine.store.save(plan)
    ci.reconcile("o/r", engine)
    plan = engine.store.load(plan.run_id)
    assert plan.status == RunStatus.ESCALATED and "CI job stopped" in plan.escalation["reason"]
    assert any("needs you" in t for _, t in gh.posted)
    gh.say(7, 3, "/sdlc resume")
    ci.reconcile("o/r", engine)
    assert engine.store.load(plan.run_id).status != RunStatus.ESCALATED


def test_help_for_unknown_command(engine, monkeypatch, tmp_path):
    gh = FakeGitHub()
    gh.install(monkeypatch, tmp_path)
    gh.issues = [ISSUE]
    gh.say(7, 9, "/sdlc dance")
    ci.reconcile("o/r", engine)
    assert any("SDLC pipeline commands" in t for _, t in gh.posted)


# ---- state persistence ---------------------------------------------------------------------------------

def test_state_round_trip_skips_rebuildable_dirs_and_rebases_paths(home, tmp_path, monkeypatch):
    run = home / "run-1"
    for rel in ("state.json", "workspace/src/a.kt", "workspace/build/out.class", "gradle/agent/x",
                "verify/attempt-1/log", "claude-home/skills/s/SKILL.md", "claude-home/projects/p.jsonl"):
        (run / rel).parent.mkdir(parents=True, exist_ok=True)
        (run / rel).write_text(str(ci.HOME / "runs/run-1/artifacts/x") if rel == "state.json" else "x")
    (home / "_cache" / "big").parent.mkdir(parents=True, exist_ok=True)
    (home / "_cache" / "big").write_text("x")
    blob = ci.pack()
    assert b"workspace" not in blob            # encrypted

    new_home = tmp_path / "elsewhere"
    monkeypatch.setattr(ci, "HOME", new_home)
    monkeypatch.setattr(ci, "RUNS", new_home / "runs")
    meta = ci.unpack(blob)
    got = new_home / "runs" / "run-1"
    assert meta["home"] == str(tmp_path / "home")
    assert (got / "workspace/src/a.kt").exists() and (got / "claude-home/projects/p.jsonl").exists()
    for gone in ("workspace/build", "gradle", "verify", "claude-home/skills"):
        assert not (got / gone).exists(), gone
    assert not (new_home / "runs" / "_cache").exists()
    assert str(new_home) in (got / "state.json").read_text()


def test_restore_skips_unsafe_members(home):
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        meta = json.dumps({"home": str(ci.HOME)}).encode()
        info = tarfile.TarInfo("_meta.json")
        info.size = len(meta)
        tar.addfile(info, io.BytesIO(meta))
        link = tarfile.TarInfo("runs/run-1/workspace/evil")
        link.type, link.linkname = tarfile.SYMTYPE, "/etc/passwd"
        tar.addfile(link)
        ok = tarfile.TarInfo("runs/run-1/state.json")
        ok.size = 2
        tar.addfile(ok, io.BytesIO(b"{}"))
    ci.unpack(ci._box().encrypt(buf.getvalue()))
    assert (home / "run-1" / "state.json").exists()
    assert not (home / "run-1" / "workspace" / "evil").exists()


# ---- sdlc init's scaffold -------------------------------------------------------------------------------

def test_scaffold_empty_repo_with_skeleton_and_ci(tmp_path):
    created = init_repo.scaffold(tmp_path, "acme/orders-service", "main", "KAN", skeleton="kotlin-http4k",
                                 package="com.acme.orders")
    app = tmp_path / "src/main/kotlin/com/acme/orders/Application.kt"
    assert app.read_text().startswith("package com.acme.orders")
    assert (tmp_path / "gradlew").stat().st_mode & 0o111
    assert (tmp_path / "gradle/wrapper/gradle-wrapper.jar").read_bytes()[:2] == b"PK"
    cfg = (tmp_path / ".sdlc/pipeline.yaml").read_text()
    assert "project_key: KAN" in cfg and "./gradlew --offline" in cfg and "kotlin-service-standards" in cfg
    assert (tmp_path / "openspec/config.yaml").exists()
    assert (tmp_path / ".claude/skills/openspec-propose/SKILL.md").exists()
    workflow = (tmp_path / ".github/workflows/sdlc.yml").read_text()
    assert "sdlc ci reconcile" in workflow and "{{version}}" not in workflow and "{{install}}" not in workflow
    assert "uv tool install \"git+https://" in workflow
    assert (tmp_path / ".github/ISSUE_TEMPLATE/sdlc-feature.yml").exists()
    assert "rootProject.name = \"orders-service\"" in (tmp_path / "settings.gradle.kts").read_text()
    assert init_repo.scaffold(tmp_path, "acme/orders-service", "main", "KAN") == []   # re-run: nothing to do


def test_scaffold_refuses_a_skeleton_over_an_existing_build(tmp_path):
    (tmp_path / "pom.xml").write_text("<project/>")
    with pytest.raises(ValueError, match="already has a build"):
        init_repo.scaffold(tmp_path, "acme/x", "main", None, skeleton="kotlin-http4k")


def test_scaffold_without_ci_or_build_has_no_verify(tmp_path):
    init_repo.scaffold(tmp_path, "acme/x", "main", None, ci=False)
    assert not (tmp_path / ".github").exists()
    assert "verify" not in (tmp_path / ".sdlc/pipeline.yaml").read_text()


def test_package_name_is_a_valid_identifier():
    assert init_repo.package_name("Auranestt-candles/workflow-test") == "com.auranesttcandles.workflowtest"
    assert init_repo.package_name("1org/x").startswith("com.app.")


# ---- infra ---------------------------------------------------------------------------------------------

def test_linux_runs_containers_as_the_host_user(monkeypatch):
    monkeypatch.setattr(infra.sys, "platform", "linux")
    monkeypatch.setattr(infra.os, "getuid", lambda: 1001)
    monkeypatch.setattr(infra.os, "getgid", lambda: 121)
    assert infra.user_args() == ["--user", "1001:121", "-e", "HOME=/tmp"]
    monkeypatch.setattr(infra.sys, "platform", "darwin")
    assert infra.user_args() == []


def test_images_expand_the_version(monkeypatch):
    from sdlc.config import load_config
    monkeypatch.setattr("sdlc.config.version", lambda: "9.9.9")
    imgs = infra.images(load_config(env={}))
    assert imgs["worker"].endswith("sdlc-worker:9.9.9") and imgs["gateway"].endswith("sdlc-gateway:9.9.9")
    assert infra.images(load_config(env={"SDLC_WORKER_IMAGE": "sdlc-worker:dev"}))["worker"] == "sdlc-worker:dev"


# ---- multi-repo features in CI mode -------------------------------------------------------------------------

REGISTRY = """repos:
  notes-api: {github: o/api, description: API, owns: [notes]}
  notes-client: {github: o/client, description: client, owns: [client], depends_on: [notes-api]}
"""
FEATURE = {**ISSUE, "number": 8, "body": ISSUE["body"] + "\n\n### Repos\n\nnotes-client, notes-api"}


def test_issue_repos_field_is_parsed_and_kept_out_of_the_requirement():
    assert ci.issue_repos(FEATURE) == ["notes-client", "notes-api"]
    assert ci.issue_repos(ISSUE) == [] and ci.issue_repos({**ISSUE, "body": "### Repos\n\n_No response_"}) == []
    assert "Repos" not in ci.requirement_text(FEATURE) and "### Goals" in ci.requirement_text(FEATURE)


def test_multi_repo_issue_starts_a_feature_plan_from_the_provider(engine, monkeypatch, tmp_path):
    gh = FakeGitHub()
    gh.install(monkeypatch, tmp_path)
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "repos.yaml").write_text(REGISTRY)
    gh.issues = [FEATURE]
    ci.reconcile("o/home", engine)
    plan = ci.runs_for_issue(engine.store, 8, "plan")[0]
    assert plan.inputs["repos"] == ["notes-client", "notes-api"] and plan.inputs["repo"] == "o/api"


def test_multi_repo_issue_needs_known_repos_and_a_cross_repo_token(engine, monkeypatch, tmp_path):
    gh = FakeGitHub()
    gh.install(monkeypatch, tmp_path)
    (tmp_path / ".sdlc").mkdir()
    (tmp_path / ".sdlc" / "repos.yaml").write_text(REGISTRY)
    gh.issues = [{**FEATURE, "body": FEATURE["body"].replace("notes-api", "billing")}]
    ci.reconcile("o/home", engine)
    assert not engine.store.list() and any("unknown repos ['billing']" in t for _, t in gh.posted)

    gh.issues, gh.posted = [FEATURE], []
    monkeypatch.setenv("SDLC_ACTIONS_TOKEN", "same")
    monkeypatch.setenv("GITHUB_TOKEN", "same")       # no SDLC_GITHUB_TOKEN: the workflow's own token
    ci.reconcile("o/home", engine)
    assert not engine.store.list() and any("SDLC_GITHUB_TOKEN" in t for _, t in gh.posted)


def test_scaffold_member_repo_gets_only_the_relay(tmp_path):
    init_repo.scaffold(tmp_path, "o/client", "main", "KAN", relay_to="o/home")
    relay = (tmp_path / ".github/workflows/sdlc-relay.yml").read_text()
    assert 'repos/o/home/dispatches' in relay and "SDLC_RELAY_TOKEN" in relay and "{{" not in relay.replace("${{", "")
    assert not (tmp_path / ".github/workflows/sdlc.yml").exists()
    assert not (tmp_path / ".github/ISSUE_TEMPLATE").exists()


def test_scaffold_feature_home_validates_and_writes_the_registry(tmp_path):
    reg = tmp_path / "repos.yaml"
    reg.write_text(REGISTRY)
    ws = tmp_path / "ws"
    ws.mkdir()
    assert ".sdlc/repos.yaml" in init_repo.scaffold(ws, "o/home", "main", "KAN", registry_file=reg)
    bad = tmp_path / "bad.yaml"
    bad.write_text("repos:\n  a: {github: o/a, depends_on: [nope]}\n")
    with pytest.raises(Exception, match="unknown repo"):
        init_repo.scaffold(tmp_path / "ws2", "o/home", "main", "KAN", registry_file=bad)
