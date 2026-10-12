"""The stage graph, as data (PLAN.md §14). (stage, outcome) → next step.

A next step is a stage name, a Loop (bounded by a config key, escalates when hit),
a Gate (pauses the run until a human event), or END / ESCALATE / ABORT.
"""

from __future__ import annotations

from dataclasses import dataclass, field

END = "__end__"
ESCALATE = "__escalate__"
ABORT = "__abort__"


@dataclass(frozen=True)
class Loop:
    to: str
    limit: str | None = None           # config key; its value None/absent means no cap
    then: str = ESCALATE


@dataclass(frozen=True)
class Gate:
    name: str
    events: dict = field(hash=False)   # event → next step
    enabled: str | None = None         # config key; when false the gate is skipped
    skip_to: str | None = None


Next = str | Loop | Gate


@dataclass(frozen=True)
class Pipeline:
    name: str
    start: str
    transitions: dict = field(hash=False)


H0_ANSWERS = Gate("h0_answers", {
    "answered": Loop("a1_grill", "grill.max_rounds"),
    "done": "a1_grill",
    "abort": ABORT,
})
H1_SPEC_REVIEW = Gate("h1_spec_review", {
    "approved": "a4_publish",
    "changes": Loop("a2_propose", "loops.spec_feedback_max"),
    "abort": ABORT,
})
H2_PLAN = Gate("h2_plan", {
    "approved": "b3_implement",
    "changes": ESCALATE,               # design changes go back through Pipeline A
    "abort": ABORT,
}, enabled="gates.plan", skip_to="b3_implement")
H3_PR_REVIEW = Gate("h3_pr_review", {
    "changes": Loop("b8_fix", "loops.mr_feedback_max"),
    "approved": "b9_archive",
    "abort": ABORT,
})

H4_MERGE = Gate("h4_merge", {        # after the archive commit: wait for the human merge
    "merged": "b10_close",
    "abort": ABORT,
})

GATES = {g.name: g for g in (H0_ANSWERS, H1_SPEC_REVIEW, H2_PLAN, H3_PR_REVIEW, H4_MERGE)}

PLAN = Pipeline("plan", start="a0_intake", transitions={
    ("a0_intake", "ok"): "a1_grill",
    ("a0_intake", "blocked"): ESCALATE,
    ("a1_grill", "needs_input"): H0_ANSWERS,
    ("a1_grill", "ready"): "a2_propose",
    ("a1_grill", "incomplete"): Loop("a1_grill", "grill.checklist_retries"),   # clarity check failed
    ("a2_propose", "ok"): "a3_validate",
    ("a3_validate", "ok"): "a3b_spec_pr",
    ("a3b_spec_pr", "ok"): H1_SPEC_REVIEW,
    ("a3_validate", "invalid"): Loop("a2_propose", "loops.spec_feedback_max"),
    ("a4_publish", "ok"): END,
})

IMPLEMENT = Pipeline("implement", start="b0_fetch", transitions={
    ("b0_fetch", "ok"): "b1_prep",
    ("b1_prep", "ok"): H2_PLAN,
    ("b3_implement", "ok"): "b4_verify",
    ("b3_implement", "blocked"): ESCALATE,   # e.g. needs a new dependency, or the spec contradicts the code
    ("b4_verify", "pass"): "b5_review",
    ("b4_verify", "fail"): Loop("b3_implement", "loops.verify_retries"),
    ("b5_review", "ok"): "b6_guard",
    ("b5_review", "blocking"): Loop("b3_implement", "loops.review_retries"),
    ("b6_guard", "pass"): "b7_pr",
    ("b6_guard", "violation"): ESCALATE,
    ("b7_pr", "ok"): H3_PR_REVIEW,
    ("b8_fix", "ok"): "b4_verify",
    ("b9_archive", "archived"): H4_MERGE,          # archive commit added to the open PR
    ("b9_archive", "closed_followup"): "b10_close",  # PR was already merged; archive went in a follow-up PR
    ("b10_close", "ok"): END,
})

PIPELINES = {p.name: p for p in (PLAN, IMPLEMENT)}
