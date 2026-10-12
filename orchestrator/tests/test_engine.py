from pathlib import Path

import pytest

from sdlc import answers
from sdlc.config import load_config
from sdlc.engine import Engine, EngineError, RunStatus, RunStore

REQ = Path(__file__).resolve().parents[2] / "demo" / "requirements.md"


@pytest.fixture
def engine(tmp_path):
    return Engine(RunStore(tmp_path / "runs"))


def start_plan(engine, overrides=None, fails=None):
    return engine.start("plan", load_config(overrides=overrides or {}), {"requirements": str(REQ)},
                        fake=True, fake_failures=fails or {})


def start_impl(engine, overrides=None, fails=None):
    return engine.start("implement", load_config(overrides=overrides or {}), {"ticket": "FAKE-42"},
                        fake=True, fake_failures=fails or {})


def stages_run(state):
    return [h.stage for h in state.history]


# ---- Pipeline A ----------------------------------------------------------------------

def test_plan_pauses_for_grill_answers_and_writes_answers_md(engine):
    state = start_plan(engine)
    assert state.status == RunStatus.WAITING_HUMAN and state.wait.gate == "h0_answers"
    assert (engine.store.run_dir(state.run_id) / "answers.md").exists()
    intake = engine.store.run_dir(state.run_id) / "artifacts/a0_intake/attempt-1/intake.json"
    assert '"Users"' in intake.read_text()  # demo requirement is missing a Users section


def test_plan_runs_end_to_end_through_both_gates(engine):
    state = start_plan(engine)
    qs = state.wait.info["questions"]
    state = engine.deliver(state.run_id, "answered", {"answers": answers.defaults(qs)})
    assert state.wait.gate == "h1_spec_review"
    state = engine.deliver(state.run_id, "approved")
    assert state.status == RunStatus.DONE
    assert stages_run(state) == ["a0_intake", "a1_grill", "a1_grill", "a2_propose", "a3_validate", "a3b_spec_pr",
                                 "a4_publish"]


def test_spec_review_changes_loop_back_to_propose_with_feedback(engine):
    state = start_plan(engine)
    state = engine.deliver(state.run_id, "answered", {"answers": []})
    state = engine.deliver(state.run_id, "changes", {"text": "split burst handling out"})
    assert state.wait.gate == "h1_spec_review"
    proposal = engine.store.run_dir(state.run_id) / "artifacts/a2_propose/attempt-2/openspec/changes/add-rate-limiting/proposal.md"
    assert "split burst handling out" in proposal.read_text()


def test_validation_failure_loops_to_propose(engine):
    state = start_plan(engine, fails={"a3_validate": 1})
    state = engine.deliver(state.run_id, "answered", {"answers": []})
    assert stages_run(state)[-5:] == ["a2_propose", "a3_validate", "a2_propose", "a3_validate", "a3b_spec_pr"]
    assert state.wait.gate == "h1_spec_review"


def test_grill_until_clear_has_no_round_cap(engine):
    state = start_plan(engine, fails={"a1_grill": 7})  # 8 rounds of questions
    for _ in range(8):
        assert state.wait.gate == "h0_answers"
        state = engine.deliver(state.run_id, "answered", {"answers": []})
    assert state.wait.gate == "h1_spec_review"


def test_bounded_grill_escalates_at_max_rounds(engine):
    state = start_plan(engine, overrides={"grill.mode": "bounded", "grill.max_rounds": 2}, fails={"a1_grill": 5})
    state = engine.deliver(state.run_id, "answered", {"answers": []})
    state = engine.deliver(state.run_id, "answered", {"answers": []})
    state = engine.deliver(state.run_id, "answered", {"answers": []})
    assert state.status == RunStatus.ESCALATED
    assert "grill.max_rounds=2" in state.escalation["reason"]


def test_done_ends_grilling_early(engine):
    state = start_plan(engine, fails={"a1_grill": 5})
    state = engine.deliver(state.run_id, "done", {"answers": []})
    assert state.wait.gate == "h1_spec_review"


def test_wrong_event_for_gate_is_rejected(engine):
    state = start_plan(engine)
    with pytest.raises(EngineError, match="accepts"):
        engine.deliver(state.run_id, "approved")


def test_duplicate_event_ids_are_ignored(engine):
    state = start_plan(engine)
    engine.deliver(state.run_id, "answered", {"answers": []}, event_id="gh-1")
    state = engine.store.load(state.run_id)
    assert state.wait.gate == "h1_spec_review"
    state = engine.deliver(state.run_id, "approved", event_id="gh-1")  # replayed id
    assert state.status == RunStatus.WAITING_HUMAN


# ---- Pipeline B ----------------------------------------------------------------------

def test_implement_skips_optional_plan_gate_and_waits_for_pr_review(engine):
    state = start_impl(engine)
    assert state.wait.gate == "h3_pr_review"
    assert "h2_plan" not in stages_run(state)


def test_plan_gate_runs_when_switched_on(engine):
    state = start_impl(engine, overrides={"gates.plan": True})
    assert state.wait.gate == "h2_plan"


def test_verify_failure_loops_then_passes(engine):
    state = start_impl(engine, fails={"b4_verify": 2})
    assert stages_run(state).count("b3_implement") == 3
    assert state.wait.gate == "h3_pr_review"
    fb = engine.store.run_dir(state.run_id) / "artifacts/b3_implement/attempt-2/impl-summary.json"
    assert "header missing" in fb.read_text()


def test_verify_retries_exhausted_escalates(engine):
    state = start_impl(engine, fails={"b4_verify": 10})
    assert state.status == RunStatus.ESCALATED
    assert "loops.verify_retries=3" in state.escalation["reason"]
    assert stages_run(state).count("b4_verify") == 4


def test_resume_from_stage_after_escalation(engine):
    state = start_impl(engine, fails={"b4_verify": 4})
    assert state.status == RunStatus.ESCALATED
    state = engine.resume(state.run_id, "b3_implement")  # 5th verify passes
    assert state.wait.gate == "h3_pr_review"


def test_pr_comments_loop_through_fix_verify_review(engine):
    state = start_impl(engine)
    state = engine.deliver(state.run_id, "changes", {"text": "exempt /health"})
    assert state.wait.gate == "h3_pr_review"
    assert stages_run(state)[-5:] == ["b8_fix", "b4_verify", "b5_review", "b6_guard", "b7_pr"]
    state = engine.deliver(state.run_id, "approved")
    assert state.wait.gate == "h4_merge" and stages_run(state)[-1] == "b9_archive"
    state = engine.deliver(state.run_id, "merged")
    assert state.status == RunStatus.DONE and stages_run(state)[-1] == "b10_close"


def test_guardrail_violation_escalates_without_retry(engine):
    state = start_impl(engine, fails={"b6_guard": 1})
    assert state.status == RunStatus.ESCALATED and state.escalation["at"] == "b6_guard"


def test_abort_and_state_survives_reload(engine):
    state = start_impl(engine)
    engine.abort(state.run_id, "requirement withdrawn")
    assert engine.store.load(state.run_id).status == RunStatus.ABORTED
    with pytest.raises(EngineError):
        engine.resume(state.run_id)


def test_real_stage_not_built_yet_escalates_with_phase(engine):
    from sdlc.stages import ARRIVES_IN
    ARRIVES_IN["b99_future"] = "P42"
    from sdlc.stages import get_stage
    with pytest.raises(NotImplementedError, match="arrives in P42"):
        get_stage("b99_future", fake=False)


def test_fake_stages_fill_in_for_unbuilt_ones(engine):
    from sdlc.engine.runner import stages_of
    state = engine.start("implement", load_config(), {"ticket": "X-1"}, fake=False,
                         fake_stages=stages_of("implement"))
    assert state.wait.gate == "h3_pr_review"


def test_config_is_frozen_per_run(engine):
    state = start_impl(engine, overrides={"loops.verify_retries": 1})
    assert engine.config_for(state).get("loops.verify_retries") == 1


def test_resume_can_raise_limits_under_org_locks(engine):
    state = start_impl(engine, fails={"b4_verify": 10})
    assert state.status == RunStatus.ESCALATED
    with pytest.raises(Exception, match="locked"):
        engine.resume(state.run_id, "b3_implement", {"gates.mr_review": False})
    state = engine.resume(state.run_id, "b3_implement", {"loops.verify_retries": 20})
    assert state.wait.gate == "h3_pr_review"
    assert engine.config_for(state).explain("loops.verify_retries")[0][2] == "run (resume)"


def test_feedback_survives_a_crashing_stage(tmp_path):
    from sdlc.stages import get_stage
    calls = []

    def lookup(name, fake=False):
        stage = get_stage(name, fake=True)
        if name == "b8_fix" and not calls:
            calls.append(1)
            raise RuntimeError("sandbox died")
        return stage

    eng = Engine(RunStore(tmp_path / "runs"), stage_lookup=lookup)
    state = eng.start("implement", load_config(), {"ticket": "X"}, fake=True)
    state = eng.deliver(state.run_id, "changes", {"text": "exempt /health"})
    assert state.status == RunStatus.ESCALATED
    assert state.feedback["b8_fix"][0]["data"]["text"] == "exempt /health"
    state = eng.resume(state.run_id)
    replies = eng.store.run_dir(state.run_id) / "artifacts/b8_fix/attempt-2/replies.json"
    assert "exempt /health" in replies.read_text()


def test_watch_marks_stale_runs_and_records_reminders(engine, monkeypatch):
    from datetime import datetime, timedelta, timezone
    from sdlc import watch
    state = start_plan(engine)   # waiting at h0 (no PR → reminders are events only)
    state.wait.since = (datetime.now(timezone.utc) - timedelta(hours=30)).isoformat(timespec="seconds")
    engine.store.save(state)
    assert any("reminder" in a for a in watch.nudge_waiting(engine))
    assert engine.store.load(state.run_id).wait.info["reminders_sent"] == [24]
    assert watch.nudge_waiting(engine) == []          # not sent twice
    state = engine.store.load(state.run_id)
    state.wait.since = (datetime.now(timezone.utc) - timedelta(days=15)).isoformat(timespec="seconds")
    engine.store.save(state)
    assert any("stale" in a for a in watch.nudge_waiting(engine))
    assert engine.store.load(state.run_id).status == RunStatus.ESCALATED


def test_chaos_external_500_escalates_then_resumes(tmp_path):
    from sdlc.connectors.jira import JiraError
    from sdlc.stages import get_stage
    outage = {"left": 1}

    def lookup(name, fake=False):
        if name == "b0_fetch" and outage["left"]:
            outage["left"] -= 1
            raise JiraError("Jira GET /rest/api/3/issue/X-1: 500 Internal Server Error")
        return get_stage(name, fake=True)

    eng = Engine(RunStore(tmp_path / "runs"), stage_lookup=lookup)
    state = eng.start("implement", load_config(), {"ticket": "X-1"}, fake=True)
    assert state.status == RunStatus.ESCALATED and "500" in state.escalation["reason"]
    state = eng.resume(state.run_id)          # Jira is back
    assert state.wait.gate == "h3_pr_review"
    assert [h.outcome for h in state.history if h.stage == "b0_fetch"] == ["error", "ok"]
