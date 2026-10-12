import json
from pathlib import Path

import pytest

from sdlc import answers, gateway_keys
from sdlc.config import load_config
from sdlc.engine import Engine, RunStatus, RunStore
from sdlc.sandbox import SandboxResult
from sdlc.stages import pipeline_a
from sdlc.stages.pipeline_a import CHECKLIST_ITEMS, clarity_problems, scan_text


@pytest.fixture(autouse=True)
def gateway_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_GATEWAY_DATA", str(tmp_path / "gw"))


def full_checklist(status="clear"):
    return {"checklist": [{"item": i, "status": status, "evidence": ["q1"]} for i in CHECKLIST_ITEMS]}


# ---- helpers ---------------------------------------------------------------------------------

def test_scan_text_finds_secrets_and_redacts_emails():
    text = "Contact ops@example.com. token: abcdefghijklmnopqrstuvwxyz123456\nkey sk-or-v1-0123456789abcdef0123"
    redacted, found, emails = scan_text(text)
    assert "openrouter_key" in found and "generic_secret" in found
    assert emails == 1 and "ops@example.com" not in redacted


def test_clarity_check():
    assert clarity_problems(full_checklist()) == []
    cl = full_checklist()
    cl["checklist"][0]["status"] = "open"
    cl["checklist"][1]["evidence"] = []
    del cl["checklist"][2]
    problems = clarity_problems(cl)
    assert any("still open" in p for p in problems)
    assert any("no evidence" in p for p in problems)
    assert any("missing" in p for p in problems)


def test_gateway_keys_reuse_revoke_and_usage(tmp_path):
    k1 = gateway_keys.key_for("run-1", "a1_grill", 1.5)
    assert gateway_keys.key_for("run-1", "a1_grill", 1.5) == k1
    assert gateway_keys.key_for("run-1", "a2_propose", 0.6) != k1
    offset = gateway_keys.usage_offset()
    usage = gateway_keys.data_dir() / "usage.jsonl"
    usage.write_text(json.dumps({"run_id": "run-1", "stage": "a1_grill", "input_tokens": 5, "output_tokens": 7,
                                 "cache_read_tokens": 100, "cost_usd": 0.01, "upstream_model": "m"}) + "\n")
    got = gateway_keys.usage_since(offset, "run-1", "a1_grill")
    assert got["calls"] == 1 and got["output_tokens"] == 7 and got["cost_usd"] == 0.01
    assert gateway_keys.run_spent("run-1") == 0.01
    assert gateway_keys.revoke_run("run-1") == 2


# ---- stages with the sandbox stubbed --------------------------------------------------------------

def requirements(tmp_path, body="# X\n## Problem\np\n## Users\nu\n## Goals\ng\n") -> Path:
    path = tmp_path / "req.md"
    path.write_text(body)
    return path


def start(tmp_path, req: Path, monkeypatch, script):
    """Run the plan pipeline with real A0/A1 and a scripted sandbox; A2+ stay fake."""
    prompts = []

    def fake_run_agent(ctx, task, io_name="io"):
        prompts.append(task.prompt)
        out = ctx.out_dir / io_name / "out"
        out.mkdir(parents=True, exist_ok=True)
        status, files = script[len(prompts) - 1]
        for name, content in files.items():
            (out / name).write_text(json.dumps(content) if not isinstance(content, str) else content)
        ctx.run.sessions[ctx.stage] = "sess-1"
        return SandboxResult(status=status, out_dir=out, data={"session_id": "sess-1"},
                             usage={"cost_usd": 0.01, "calls": 1})

    monkeypatch.setattr(pipeline_a, "_run_agent", fake_run_agent)
    monkeypatch.setattr(pipeline_a, "ensure_workspace", lambda *a, **k: tmp_path)
    engine = Engine(RunStore(tmp_path / "runs"))
    fakes = ["a2_propose", "a3_validate", "a3b_spec_pr", "a4_publish"]
    state = engine.start("plan", load_config(), {"requirements": str(req), "project": str(tmp_path)},
                         fake_stages=fakes)
    return engine, state, prompts


def test_intake_blocks_secrets(tmp_path, monkeypatch):
    req = requirements(tmp_path, "## Problem\nuse key AKIAABCDEFGHIJKLMNOP\n")
    engine, state, _ = start(tmp_path, req, monkeypatch, [])
    assert state.status == RunStatus.ESCALATED and "aws_access_key" in state.escalation["reason"]


def test_grill_question_answer_resume_and_finish(tmp_path, monkeypatch):
    q = {"status": "needs_input", "questions": [{"id": "q1", "text": "Who is limited?", "recommended": "per key",
                                                  "audience": "po"}]}
    script = [("needs_input", {"questions.json": q}),
              ("ok", {"qa-log.md": "# log", "checklist.json": full_checklist()})]
    engine, state, prompts = start(tmp_path, requirements(tmp_path), monkeypatch, script)
    assert state.wait.gate == "h0_answers"
    assert state.wait.info["questions"][0] == {"id": "r1-q1", "text": "Who is limited?", "default": "per key",
                                               "audience": "po", "why": None}
    assert prompts[0].startswith("/grill-with-docs")
    qs = state.wait.info["questions"]
    state = engine.deliver(state.run_id, "answered", {"answers": answers.from_values(qs, {"r1-q1": "per IP"})})
    assert "Q: Who is limited?" in prompts[1] and "A: per IP" in prompts[1] and "their own answer" in prompts[1]
    assert state.wait.gate == "h1_spec_review"
    assert state.history[1].usage["cost_usd"] == 0.01


def test_grill_finish_with_open_items_is_sent_back(tmp_path, monkeypatch):
    open_cl = full_checklist()
    open_cl["checklist"][3]["status"] = "open"
    script = [("ok", {"qa-log.md": "x", "checklist.json": open_cl}),
              ("ok", {"qa-log.md": "x", "checklist.json": full_checklist()})]
    engine, state, prompts = start(tmp_path, requirements(tmp_path), monkeypatch, script)
    assert "clarity check rejected" in prompts[1] and "acceptance_criteria still open" in prompts[1]
    assert state.wait.gate == "h1_spec_review"


def test_grill_done_prompt(tmp_path, monkeypatch):
    q = {"status": "needs_input", "questions": [{"id": "q1", "text": "t", "recommended": "r"}]}
    script = [("needs_input", {"questions.json": q}),
              ("ok", {"qa-log.md": "x", "checklist.json": full_checklist("assumed")})]
    engine, state, prompts = start(tmp_path, requirements(tmp_path), monkeypatch, script)
    engine.deliver(state.run_id, "done", {"answers": []})
    assert prompts[1].startswith("The human said: done")


def test_listed_changes_parses_and_rejects_bad_json(tmp_path):
    good = tmp_path / "c.json"
    good.write_text(json.dumps({"changes": [{"id": "a"}, {"id": "b"}]}))
    assert pipeline_a.listed_changes(good) == {"a", "b"}
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    with pytest.raises(pipeline_a.SandboxError):
        pipeline_a.listed_changes(bad)
