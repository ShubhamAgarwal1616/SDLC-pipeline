"""Grill bounded mode and the repeated-question filter, requirement updates, repo instructions, follow-up reviews."""

import json
import subprocess
from pathlib import Path

import pytest

from sdlc import answers
from sdlc.config import load_config
from sdlc.engine import Engine, RunState, RunStatus, RunStore
from sdlc.engine.stage import StageContext
from sdlc.sandbox import SandboxResult
from sdlc.stages import pipeline_a, pipeline_b_review
from sdlc.stages.pipeline_a import CHECKLIST_ITEMS, readiness, similarity


@pytest.fixture(autouse=True)
def gateway_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_GATEWAY_DATA", str(tmp_path / "gw"))


def checklist(open_items=0):
    return [{"item": i, "status": "open" if n < open_items else "clear", "evidence": ["q1"]}
            for n, i in enumerate(CHECKLIST_ITEMS)]


def question(qid, text):
    return {"status": "needs_input", "questions": [{"id": qid, "text": text, "recommended": "r"}],
            "checklist": checklist(open_items=5)}


def run_plan(tmp_path, monkeypatch, script, overrides=None, req_text="# X\n## Problem\np\n## Users\nu\n## Goals\ng\n"):
    prompts = []

    def fake_run_agent(ctx, task, io_name="io", session_key=None):
        prompts.append(task.prompt)
        out = ctx.out_dir / io_name / "out"
        out.mkdir(parents=True, exist_ok=True)
        status, files = script[len(prompts) - 1]
        for name, content in files.items():
            (out / name).write_text(json.dumps(content) if not isinstance(content, str) else content)
        ctx.run.sessions[ctx.stage] = "sess-1"
        return SandboxResult(status=status, out_dir=out, data={"session_id": "sess-1"}, usage={"cost_usd": 0.01})

    monkeypatch.setattr(pipeline_a, "_run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline_a, "ensure_workspace", lambda *a, **k: tmp_path)
    req = tmp_path / "req.md"
    req.write_text(req_text)
    engine = Engine(RunStore(tmp_path / "runs"))
    state = engine.start("plan", load_config(overrides=overrides or {}, env={}),
                         {"requirements": str(req), "project": str(tmp_path)},
                         fake_stages=["a2_propose", "a3_validate", "a3b_spec_pr", "a4_publish"])
    return engine, state, prompts


def finish():
    return ("ok", {"qa-log.md": "log", "checklist.json": {"checklist": checklist()}})


# ---- helpers ------------------------------------------------------------------------------------------------

def test_similarity_and_readiness():
    assert similarity("Should the client retry on 429?", "Should the client retry automatically on a 429?") >= 0.6
    assert similarity("Who is rate limited?", "What database do we use?") < 0.2
    assert readiness(checklist(open_items=2)) == 0.8


# ---- repeated questions --------------------------------------------------------------------------------------

def test_repeated_question_is_dropped_and_its_answer_fed_back(tmp_path, monkeypatch):
    script = [("needs_input", {"questions.json": question("q1", "Should the client retry on a 429 response?")}),
              ("needs_input", {"questions.json": {
                  "status": "needs_input", "checklist": checklist(3),
                  "questions": [{"id": "q2", "text": "Should the client retry on a 429 response?", "recommended": "x"},
                                {"id": "q3", "text": "How many retries at most?", "recommended": "3"}]}}),
              finish()]
    engine, state, prompts = run_plan(tmp_path, monkeypatch, script)
    qs = state.wait.info["questions"]
    state = engine.deliver(state.run_id, "answered", {"answers": answers.from_values(qs, {"r1-q1": "yes, 3 times"})})
    assert [q["text"] for q in state.wait.info["questions"]] == ["How many retries at most?"]
    state = engine.deliver(state.run_id, "answered", {"answers": answers.defaults(state.wait.info["questions"])})
    assert "already answered" in prompts[2] and "yes, 3 times" in prompts[2]
    assert state.wait.gate == "h1_spec_review"


def test_round_of_only_repeats_stops_the_grill(tmp_path, monkeypatch):
    script = [("needs_input", {"questions.json": question("q1", "Which clients are limited?")}),
              ("needs_input", {"questions.json": question("q2", "Which clients are limited?")}),
              finish()]
    engine, state, prompts = run_plan(tmp_path, monkeypatch, script)
    state = engine.deliver(state.run_id, "answered", {"answers": answers.defaults(state.wait.info["questions"])})
    assert "Stop grilling now: every question it asked was already answered" in prompts[2]
    assert state.wait.gate == "h1_spec_review"


# ---- bounded mode -------------------------------------------------------------------------------------------

def test_bounded_mode_stops_after_max_rounds(tmp_path, monkeypatch):
    script = [("needs_input", {"questions.json": question("q1", "Alpha question about scope?")}),
              finish()]
    engine, state, prompts = run_plan(tmp_path, monkeypatch, script,
                                      {"grill.mode": "bounded", "grill.max_rounds": 1})
    engine.deliver(state.run_id, "answered", {"answers": answers.defaults(state.wait.info["questions"])})
    assert "this project allows 1 rounds of questions" in prompts[1]


def test_bounded_mode_readiness_threshold_skips_asking(tmp_path, monkeypatch):
    nearly = {"status": "needs_input", "checklist": checklist(open_items=1),
              "questions": [{"id": "q1", "text": "Minor detail about logging format?", "recommended": "json"}]}
    almost_done = {"checklist": checklist(open_items=1)}
    script = [("needs_input", {"questions.json": nearly}), ("ok", {"qa-log.md": "x", "checklist.json": almost_done})]
    engine, state, prompts = run_plan(tmp_path, monkeypatch, script,
                                      {"grill.mode": "bounded", "grill.max_rounds": 4, "grill.readiness_threshold": 0.8})
    assert "90% of the checklist is settled" in prompts[1]
    assert state.wait.gate == "h1_spec_review"     # one item still open, but the threshold allows it


def test_questions_per_round_caps_the_batch(tmp_path, monkeypatch):
    many = {"status": "needs_input", "checklist": checklist(5),
            "questions": [{"id": f"q{i}", "text": f"Distinct topic number {i} {'x' * i}?", "recommended": "r"}
                          for i in range(5)]}
    engine, state, prompts = run_plan(tmp_path, monkeypatch, [("needs_input", {"questions.json": many})],
                                      {"grill.questions_per_round": 2})
    assert len(state.wait.info["questions"]) == 2
    assert "up to 2 questions per turn" in prompts[0]


# ---- requirement updates ------------------------------------------------------------------------------------

def test_update_requirement_reruns_intake_with_a_diff_and_asks_about_the_change(tmp_path, monkeypatch):
    script = [("needs_input", {"questions.json": question("q1", "Who is limited?")}),
              ("needs_input", {"questions.json": question("q2", "What about the new burst rule?")})]
    engine, state, prompts = run_plan(tmp_path, monkeypatch, script)
    new = tmp_path / "req-v2.md"
    new.write_text("# X\n## Problem\np\n## Users\nu\n## Goals\ng\nAllow bursts of 20.\n")
    state = engine.update_requirements(state.run_id, new)
    assert prompts[1].startswith("The requirement was updated")
    diff = engine.store.run_dir(state.run_id) / "artifacts/a0_intake/attempt-2/requirements.diff"
    assert "+Allow bursts of 20." in diff.read_text()
    assert state.wait.gate == "h0_answers" and state.inputs["_req_version"] == 1


def test_update_is_refused_for_finished_runs(tmp_path, monkeypatch):
    engine, state, _ = run_plan(tmp_path, monkeypatch, [finish()])
    engine.abort(state.run_id)
    with pytest.raises(Exception, match="aborted"):
        engine.update_requirements(state.run_id, tmp_path / "req.md")


# ---- repo instructions in the contract ---------------------------------------------------------------------

def test_repo_instructions_are_appended_to_the_stage_contract(tmp_path):
    store = RunStore(tmp_path / "runs")
    cfg = load_config(env={})
    cfg.data["instructions"] = {"implement": "Use Result types."}
    ctx = StageContext(run=RunState(run_id="run-x", pipeline="implement"), config=cfg, store=store,
                       stage="b3_implement", attempt=1, feedback=[])
    combined = pipeline_a.stage_contract(ctx, "implement.md")
    text = combined.read_text()
    assert text.startswith("# Stage contract") and text.rstrip().endswith("Use Result types.")
    assert pipeline_a.stage_contract(ctx, "review.md") == pipeline_a.CONTRACTS / "review.md"


# ---- follow-up reviews ---------------------------------------------------------------------------------------

def _git(cwd, *args):
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *args], cwd=cwd, check=True,
                   capture_output=True)


def test_followup_review_uses_previous_findings_cheaper_model_and_cap(tmp_path, monkeypatch):
    src = tmp_path / "src"
    src.mkdir()
    _git(src, "init", "-q")
    (src / "a.kt").write_text("v1")
    _git(src, "add", "-A")
    _git(src, "commit", "-qm", "base")
    base = subprocess.run(["git", "rev-parse", "HEAD"], cwd=src, capture_output=True, text=True).stdout.strip()
    (src / "a.kt").write_text("v2")
    _git(src, "commit", "-qam", "change")

    seen = []

    def fake_run_agent(ctx, task, io_name="io", session_key=None):
        seen.append(task)
        out = ctx.out_dir / io_name / "out"
        out.mkdir(parents=True, exist_ok=True)
        findings = [{"severity": "major", "issue": f"bug {i}", "file": "a.kt", "line": i} for i in range(7)]
        (out / "review.json").write_text(json.dumps({"verdict": "changes_required", "findings": findings,
                                                     "scenarios": []}))
        return SandboxResult(status="ok", out_dir=out, data={}, usage={})

    monkeypatch.setattr(pipeline_b_review, "_run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline_b_review, "_latest_verify_src", lambda ctx: src)
    monkeypatch.setattr(pipeline_b_review, "current_base_sha", lambda ctx: base)
    monkeypatch.setattr(pipeline_b_review, "_ticket", lambda ctx: {"key": "K-1", "summary": "s", "change_id": "c"})
    monkeypatch.setattr(pipeline_b_review, "_prep", lambda ctx: {})
    monkeypatch.setattr(pipeline_b_review, "mount_contracts", lambda ctx, task: None)
    monkeypatch.setenv("GITHUB_TOKEN", "test-token")   # git runs with the token env; nothing is contacted
    store = RunStore(tmp_path / "runs")
    run = RunState(run_id="run-r", pipeline="implement")
    cfg = load_config(env={})
    review = pipeline_b_review.Review("b5_review")

    ctx1 = StageContext(run=run, config=cfg, store=store, stage="b5_review", attempt=1, feedback=[])
    monkeypatch.setattr(StageContext, "latest", lambda self, stage, name: None if stage != "b5_review" else
                        next((Path(h.artifacts[name]) for h in reversed(self.run.history)
                              if h.stage == stage and name in h.artifacts), None))
    r1 = review.run(ctx1)
    assert r1.outcome == "blocking" and r1.note.count("[major]") == 5           # capped at max_blocking_per_round
    first = json.loads(Path(r1.artifacts["review.json"]).read_text())
    assert first["round"] == 1 and len(first["deferred"]) == 2 and first["model"] == "claude-opus-5-5"

    from sdlc.engine.state import HistoryEntry
    run.history.append(HistoryEntry(stage="b5_review", attempt=1, outcome="blocking", started="", finished="",
                                    artifacts=r1.artifacts))
    ctx2 = StageContext(run=run, config=cfg, store=store, stage="b5_review", attempt=2, feedback=[])
    review.run(ctx2)
    task = seen[-1]
    assert task.model == "claude-sonnet-5-5"
    assert "follow-up review (round 2)" in task.prompt
    assert {"previous-review.json", "since-last.patch"} <= set(task.inputs)
