"""Fake stages for the walking skeleton (P1).

Each returns canned artifacts so every transition, loop and gate can run end to end
without Docker, an LLM, or external services. `--fake-fail stage=N` makes a stage take its
failure outcome for its first N attempts, to exercise feedback loops and escalation.
"""

from __future__ import annotations

import re
from pathlib import Path

from sdlc.engine.stage import StageContext, StageResult

NO_USAGE = {"input_tokens": 0, "output_tokens": 0, "cache_read_tokens": 0, "cost_usd": 0.0}

FAKE_QUESTIONS = [
    {"id": "q1", "text": "What limit per client?", "default": "100 req/min per API key", "audience": "po"},
    {"id": "q2", "text": "What should the response be when a client is limited?",
     "default": "429 with a Retry-After header", "audience": "tech"},
    {"id": "q3", "text": "Are any clients exempt?", "default": "None", "audience": "po"},
]


def _failing(ctx: StageContext) -> bool:
    return ctx.attempt <= ctx.run.fake_failures.get(ctx.stage, 0)


def _change_id(ctx: StageContext) -> str:
    return ctx.run.inputs.get("change_id", "add-rate-limiting")


class FakeStage:
    name = "fake"

    def __init__(self, name: str):
        self.name = name

    def result(self, outcome: str, **kw) -> StageResult:
        kw.setdefault("usage", NO_USAGE)
        return StageResult(outcome=outcome, **kw)


# ---- Pipeline A ----------------------------------------------------------------------

class Intake(FakeStage):
    """Real-ish already: checks the required headings in the requirements file."""

    def run(self, ctx: StageContext) -> StageResult:
        req = Path(ctx.run.inputs["requirements"])
        text = req.read_text()
        headings = {m.strip().lower() for m in re.findall(r"^#{1,6}\s+(.+)$", text, re.M)}
        required = ctx.config.get("intake.required_sections", [])
        missing = [s for s in required if s.lower() not in headings]
        too_big = len(text.encode()) > ctx.config.get("intake.max_bytes", 200_000)
        intake = {"run_id": ctx.run.run_id, "file": str(req), "missing_sections": missing,
                  "too_big": too_big, "redactions": 0}
        out = {"intake.json": ctx.write("intake.json", intake)}
        if too_big:
            return self.result("blocked", artifacts=out, note="requirements file exceeds intake.max_bytes")
        return self.result("ok", artifacts=out)


class Grill(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        said_done = any(f.get("event") == "done" for f in ctx.feedback)
        rounds_of_questions = 1 + ctx.run.fake_failures.get(ctx.stage, 0)
        if not said_done and ctx.attempt <= rounds_of_questions:
            questions = [{**q, "id": f"r{ctx.attempt}{q['id']}"} for q in FAKE_QUESTIONS]
            payload = {"status": "needs_input", "open": ["acceptance_criteria", "error_behaviour"],
                       "questions": questions}
            return self.result("needs_input", artifacts={"questions.json": ctx.write("questions.json", payload)},
                               gate_info={"round": ctx.attempt, "questions": questions})
        answers = [a for f in ctx.feedback for a in f.get("data", {}).get("answers", [])]
        lines = [f"- {a['id']} → {a['value'] if a['value'] is not None else '(skipped: open assumption)'}"
                 f" ({a['source']})" for a in answers]
        qa_log = "# Q&A log (fake)\n\n" + ("\n".join(lines) or "- no answers recorded") + "\n"
        checklist = {"ready": True, "checklist": [
            {"item": "problem_and_goal", "status": "clear", "evidence": ["req:L3"]},
            {"item": "acceptance_criteria", "status": "clear", "evidence": ["q1", "q2"]},
            {"item": "nfr_performance", "status": "assumed", "evidence": ["q3:default"]}]}
        return self.result("ready", artifacts={"qa-log.md": ctx.write("qa-log.md", qa_log),
                                               "checklist.json": ctx.write("checklist.json", checklist)})


class Propose(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        cid = _change_id(ctx)
        changes = "\n".join(f"- {f.get('data', {}).get('text') or f.get('note')}" for f in ctx.feedback) or "- none"
        files = {
            f"prd.md": "# PRD (fake, from to-prd)\n\n## Problem Statement\nClients overload the API.\n",
            f"openspec/changes/{cid}/proposal.md": f"# Proposal: {cid}\n\nFeedback applied this round:\n{changes}\n",
            f"openspec/changes/{cid}/tasks.md": "- [ ] 1. Add middleware\n- [ ] 2. Add tests\n",
            f"openspec/changes/{cid}/specs/api-rate-limiting/spec.md":
                "## ADDED Requirements\n### Requirement: Per-key rate limit\n"
                "#### Scenario: Limit exceeded\n- **WHEN** request 101 within 60s\n- **THEN** 429 + Retry-After\n",
        }
        return self.result("ok", artifacts={name: ctx.write(name, body) for name, body in files.items()})


class Validate(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        cid = _change_id(ctx)
        if _failing(ctx):
            report = {"change": cid, "valid": False, "issues": [
                {"level": "ERROR", "message": "Requirement 'Burst handling' has no scenario"}]}
            return self.result("invalid", artifacts={"validation.json": ctx.write("validation.json", report)},
                               note="Requirement 'Burst handling' has no scenario")
        report = {"change": cid, "valid": True, "issues": []}
        return self.result("ok", artifacts={"validation.json": ctx.write("validation.json", report)})


class SpecPr(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        info = {"review": f"spec PR (fake) for openspec/changes/{_change_id(ctx)}"}
        return self.result("ok", artifacts={"spec-pr.json": ctx.write("spec-pr.json", info)}, gate_info=info)


class Publish(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        published = {"changes": [{"id": _change_id(ctx), "jira": "FAKE-42",
                                  "confluence": "https://confluence.example/fake/AbC1"}]}
        return self.result("ok", artifacts={"published.json": ctx.write("published.json", published)})


# ---- Pipeline B ----------------------------------------------------------------------

class Fetch(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        ticket = {"key": ctx.run.inputs.get("ticket", "FAKE-42"), "summary": "Add per-key rate limiting",
                  "change_id": _change_id(ctx), "repo": "sample-repo"}
        return self.result("ok", artifacts={"ticket.json": ctx.write("ticket.json", ticket)})


class Prep(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        key = ctx.run.inputs.get("ticket", "FAKE-42")
        pack = (f"# Context: {_change_id(ctx)}\nBranch: feature/{key}-{_change_id(ctx)}\n"
                "## Relevant files\n1. src/main/kotlin/App.kt\n## Skill hints\n- kotlin-service-standards\n")
        return self.result("ok", artifacts={"context-pack.md": ctx.write("context-pack.md", pack)},
                           gate_info={"design": f"openspec/changes/{_change_id(ctx)}/design.md"})


class Implement(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        summary = {"status": "ok", "commits": [f"fake{ctx.attempt:03d}"], "tasks_done": 2, "tasks_total": 2,
                   "feedback_addressed": [f.get("note") or f.get("event") for f in ctx.feedback]}
        return self.result("ok", artifacts={"impl-summary.json": ctx.write("impl-summary.json", summary)})


class Verify(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        failing = _failing(ctx)
        report = {"passed": not failing, "checks": {"test": "fail" if failing else "pass", "lint": "pass"},
                  "feedback": "FAILED test_retry_after: header missing" if failing else ""}
        out = {"verify.json": ctx.write("verify.json", report)}
        if failing:
            return self.result("fail", artifacts=out, note=report["feedback"])
        return self.result("pass", artifacts=out)


class Review(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        blocking = _failing(ctx)
        findings = [{"severity": "major", "file": "Limit.kt", "line": 18,
                     "issue": "limit hard-coded; read from config"}] if blocking else []
        out = {"review.json": ctx.write("review.json", {"verdict": "changes_required" if blocking else "ok",
                                                         "findings": findings})}
        if blocking:
            return self.result("blocking", artifacts=out, note=findings[0]["issue"])
        return self.result("ok", artifacts=out)


class Guard(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        violating = _failing(ctx)
        report = {"passed": not violating,
                  "violations": [{"rule": "forbidden_path", "path": ".github/workflows/ci.yml"}] if violating else []}
        out = {"guard.json": ctx.write("guard.json", report)}
        if violating:
            return self.result("violation", artifacts=out, note="edited a forbidden path")
        return self.result("pass", artifacts=out)


class RaisePr(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        pr = {"url": "https://github.com/example/sample-repo/pull/1", "pushed_attempt": ctx.attempt}
        return self.result("ok", artifacts={"pr.json": ctx.write("pr.json", pr)},
                           gate_info={"pr": pr["url"]})


class Fix(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        comments = [f.get("data", {}).get("text") for f in ctx.feedback]
        replies = {"replies": [{"comment": c, "body": f"Done (fake): {c}"} for c in comments if c]}
        return self.result("ok", artifacts={"replies.json": ctx.write("replies.json", replies)})


class Archive(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        done = {"archived": f"openspec/changes/archive/{_change_id(ctx)}"}
        return self.result("archived", artifacts={"archive.json": ctx.write("archive.json", done)},
                           gate_info={"pr": "https://github.com/example/sample-repo/pull/1"})


class Close(FakeStage):
    def run(self, ctx: StageContext) -> StageResult:
        return self.result("ok", artifacts={"close.json": ctx.write("close.json", {"jira": "Done"})})


FAKE_STAGES = {
    "a0_intake": Intake, "a1_grill": Grill, "a2_propose": Propose, "a3_validate": Validate,
    "a3b_spec_pr": SpecPr, "a4_publish": Publish,
    "b0_fetch": Fetch, "b1_prep": Prep, "b3_implement": Implement, "b4_verify": Verify,
    "b5_review": Review, "b6_guard": Guard, "b7_pr": RaisePr, "b8_fix": Fix, "b9_archive": Archive, "b10_close": Close,
}
