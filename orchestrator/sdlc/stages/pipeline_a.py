"""Real Pipeline A stages (P3): A0 intake, A1 grill, A2 propose, A3 validate.

A0 is a script. A1 and A2 run Claude Code in the agent sandbox via the gateway. A3 runs the
OpenSpec CLI in a no-network tool container. Phase 1 code here only prepares inputs,
starts containers, checks results and copies artifacts.
"""

from __future__ import annotations

import difflib
import json
import re
import shutil
from pathlib import Path

from sdlc import gateway_keys
from sdlc.config import ROOT
from sdlc.engine.stage import StageContext, StageResult
from sdlc.sandbox import SandboxError, SandboxRunner, SandboxTask
from sdlc.registry import Target, plan_targets
from sdlc.workspace import ensure_workspace

CONTRACTS = ROOT / "orchestrator" / "contracts"
CHECKLIST_ITEMS = ["problem_and_goal", "users_and_roles", "scope_in_out", "acceptance_criteria", "error_behaviour",
                   "nfr_performance", "nfr_security_privacy", "dependencies", "rollout_migration", "success_metrics"]

SECRET_PATTERNS = {
    "private_key": r"-----BEGIN [A-Z ]*PRIVATE KEY-----",
    "aws_access_key": r"\bAKIA[0-9A-Z]{16}\b",
    "github_token": r"\bgh[pousr]_[A-Za-z0-9]{36,}\b",
    "openrouter_key": r"\bsk-or-[A-Za-z0-9-]{20,}",
    "anthropic_key": r"\bsk-ant-[A-Za-z0-9_-]{20,}",
    "slack_token": r"\bxox[abprs]-[A-Za-z0-9-]{10,}",
    "generic_secret": r"(?i)\b(api[_-]?key|secret|password|token)\s*[:=]\s*['\"]?[A-Za-z0-9/+_\-]{16,}",
}
EMAIL = r"[\w.+-]+@[\w-]+\.[\w.-]+"

# llm.stages.<key> / budgets.per_stage_usd.<key> for each pipeline stage
STAGE_KEY = {"a1_grill": "grill", "a2_propose": "propose", "b3_implement": "implement", "b5_review": "review",
             "b8_fix": "resolve_comments"}


# ---- helpers ------------------------------------------------------------------------------

def _runner(ctx: StageContext) -> SandboxRunner:
    return SandboxRunner(ctx.config)


def _run_dir(ctx: StageContext) -> Path:
    return ctx.store.run_dir(ctx.run.run_id)


def _check_run_budget(ctx: StageContext) -> None:
    budget = ctx.config.get("budgets.per_run_usd")
    spent = gateway_keys.run_spent(ctx.run.run_id)
    if budget is not None and spent >= budget:
        raise SandboxError(f"run budget exhausted: ${spent:.4f} of ${budget} (raise with --budget or --set)")


def stage_contract(ctx: StageContext, contract: str) -> Path:
    """The stage contract, plus the repo's own .sdlc/instructions/<stage>.md if it has one."""
    base = CONTRACTS / contract
    extra = (ctx.config.get("instructions") or {}).get(Path(contract).stem)
    if not extra:
        return base
    combined = ctx.out_dir / f"contract-{contract}"
    combined.write_text(base.read_text().rstrip() + "\n\n## Repo instructions (.sdlc/instructions/"
                        f"{contract})\nThese come from the repo's maintainers. They refine the rules above; where "
                        "they conflict, the rules above win.\n\n" + extra + "\n")
    return combined


def _agent_task(ctx: StageContext, prompt: str, contract: str, inputs: dict[str, Path],
                expected: list[str], resume: str | None) -> SandboxTask:
    key = STAGE_KEY[ctx.stage]
    return SandboxTask(stage=ctx.stage, prompt=prompt, model=ctx.config.get(f"llm.stages.{key}.model"),
                       max_turns=ctx.config.get(f"llm.stages.{key}.max_turns", 20),
                       contract=stage_contract(ctx, contract),
                       inputs=inputs, expected_outputs=expected, resume_session=resume,
                       timeout_s=ctx.config.get("sandbox.timeout_minutes", 30) * 60)


WARN_AT = 0.8


def _budget_warnings(ctx: StageContext, stage_budget: float | None) -> None:
    """Log (once per level) when a stage or the run passes 80% of its budget. `sdlc status` shows them."""
    checks = [("stage", gateway_keys.stage_spent(ctx.run.run_id, ctx.stage), stage_budget),
              ("run", gateway_keys.run_spent(ctx.run.run_id), ctx.config.get("budgets.per_run_usd"))]
    for scope, spent, budget in checks:
        if budget and spent >= WARN_AT * budget:
            tag = f"{scope}:{ctx.stage if scope == 'stage' else 'run'}"
            seen = ctx.run.inputs.setdefault("_budget_warned", [])
            if tag not in seen:
                seen.append(tag)
                ctx.store.log_event(ctx.run.run_id, "budget_warning", scope=scope, stage=ctx.stage,
                                    spent=spent, budget=budget)


def _run_agent(ctx: StageContext, task: SandboxTask, io_name: str = "io", session_key: str | None = None):
    runner = _runner(ctx)
    runner.preflight()
    _check_run_budget(ctx)
    budget = ctx.config.get(f"budgets.per_stage_usd.{STAGE_KEY[ctx.stage]}")
    _budget_warnings(ctx, budget)
    key = gateway_keys.key_for(ctx.run.run_id, ctx.stage, budget)
    result = runner.run_agent(_run_dir(ctx), task, key, ctx.out_dir / io_name)
    _budget_warnings(ctx, budget)
    if result.data.get("session_id"):
        ctx.run.sessions[session_key or ctx.stage] = result.data["session_id"]
    return result


def _add_usage(total: dict, more: dict) -> dict:
    out = dict(total)
    for k, v in more.items():
        if isinstance(v, (int, float)):
            out[k] = round(out.get(k, 0) + v, 6)
        elif isinstance(v, list):
            out[k] = sorted(set(out.get(k, [])) | set(v))
    return out


def _copy(ctx: StageContext, src: Path, name: str | None = None) -> str:
    dest = ctx.out_dir / (name or src.name)
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy(src, dest)
    return str(dest)


def listed_changes(path: Path) -> set[str]:
    try:
        return {c["id"] for c in json.loads(path.read_text()).get("changes", [])}
    except (ValueError, KeyError, TypeError) as exc:
        raise SandboxError(f"changes.json is not valid: {exc}") from exc


def scan_text(text: str) -> tuple[str, list[str], int]:
    """Return (text with emails redacted, secret kinds found, emails redacted)."""
    found = [kind for kind, pattern in SECRET_PATTERNS.items() if re.search(pattern, text)]
    redacted, emails = re.subn(EMAIL, "[redacted-email]", text)
    return redacted, found, emails


def clarity_problems(checklist: dict) -> list[str]:
    items = {i.get("item"): i for i in checklist.get("checklist", []) if isinstance(i, dict)}
    problems = [f"{name} missing" for name in CHECKLIST_ITEMS if name not in items]
    for name, item in items.items():
        status = item.get("status")
        if status == "open":
            problems.append(f"{name} still open")
        elif status in {"clear", "assumed"} and not item.get("evidence"):
            problems.append(f"{name} has no evidence")
        elif status not in {"clear", "assumed", "open"}:
            problems.append(f"{name} has unknown status {status!r}")
    return problems


# ---- A0 intake (script) ---------------------------------------------------------------------

class Intake:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        req = Path(ctx.run.inputs["requirements"])
        text = req.read_text()
        headings = {h.strip().lower() for h in re.findall(r"^#{1,6}\s+(.+)$", text, re.M)}
        missing = [s for s in ctx.config.get("intake.required_sections", []) if s.lower() not in headings]
        redacted, secrets_found, emails = scan_text(text)
        previous = ctx.latest("a0_intake", "requirements.md")   # an earlier version (sdlc plan --update)
        artifacts = {"requirements.md": ctx.write("requirements.md", redacted)}
        if previous and ctx.run.inputs.get("_req_version"):
            diff = "".join(difflib.unified_diff(previous.read_text().splitlines(keepends=True),
                                                redacted.splitlines(keepends=True), "previous", "updated"))
            artifacts["requirements.diff"] = ctx.write("requirements.diff", diff or "(no textual change)\n")
        if ctx.run.inputs.get("context"):
            ctx_text, ctx_secrets, ctx_emails = scan_text(Path(ctx.run.inputs["context"]).read_text())
            secrets_found += ctx_secrets
            emails += ctx_emails
            artifacts["context.md"] = ctx.write("context.md", ctx_text)
        too_big = len(text.encode()) > ctx.config.get("intake.max_bytes", 200_000)
        intake = {"run_id": ctx.run.run_id, "file": str(req), "missing_sections": missing,
                  "secrets_found": secrets_found, "emails_redacted": emails, "too_big": too_big}
        artifacts["intake.json"] = ctx.write("intake.json", intake)
        if secrets_found:
            return StageResult("blocked", artifacts, note=f"possible secrets in input ({', '.join(secrets_found)}); "
                                                          "remove them and start again")
        if too_big:
            return StageResult("blocked", artifacts, note="requirements file exceeds intake.max_bytes")
        return StageResult("ok", artifacts)


# ---- A1 grill (LLM: /grill-with-docs) ------------------------------------------------------------

GRILL_START = ("/grill-with-docs Clarify the requirement in /io/in/inputs/requirements.md (the user's plan). "
               "Gaps found by intake are in /io/in/inputs/intake.json. Follow the stage contract.")
GRILL_DONE = ("The human said: done — stop grilling now. Mark every remaining open item as assumed with your "
              "recommended default, and finish per the stage contract.")
GRILL_STOP = ("Stop grilling now: {why}. Mark every remaining open item as assumed with your recommended default, "
              "and finish per the stage contract (qa-log.md and checklist.json; no questions.json).")
GRILL_UPDATED = ("The requirement was updated: the new version is /io/in/inputs/requirements.md and what changed is in "
                 "/io/in/inputs/requirements.diff. Ask only about what changed. Keep earlier answers unless the change "
                 "contradicts them, and update the checklist and CONTEXT.md to match.")


def _words(text: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", text.lower()) if len(w) > 2}


def similarity(a: str, b: str) -> float:
    """Jaccard similarity of the questions' words: cheap, deterministic, good enough to catch re-asks."""
    wa, wb = _words(a), _words(b)
    return len(wa & wb) / len(wa | wb) if wa and wb else 0.0


def readiness(checklist: list[dict]) -> float:
    items = [i for i in checklist if isinstance(i, dict)]
    return sum(i.get("status") in {"clear", "assumed"} for i in items) / len(items) if items else 0.0


class Grill:
    def __init__(self, name: str):
        self.name = name

    def _answers(self, ctx: StageContext) -> list[dict]:
        return [f for f in ctx.feedback if f.get("source") == "h0_answers"]

    def _prompt(self, ctx: StageContext) -> str:
        cfg = ctx.config
        bounded = cfg.get("grill.mode") == "bounded"
        per_round = cfg.get("grill.questions_per_round")
        hint = (f" This project allows up to {per_round} questions per turn: batch the most important open ones."
                if per_round and per_round > 1 else "")
        updated = ctx.run.inputs.get("_req_version", 0) > ctx.run.inputs.get("_grill_seen_version", 0)
        if ctx.stage not in ctx.run.sessions:
            ctx.run.inputs["_grill_seen_version"] = ctx.run.inputs.get("_req_version", 0)
            return GRILL_START + hint
        if updated:
            ctx.run.inputs["_grill_seen_version"] = ctx.run.inputs.get("_req_version", 0)
            return GRILL_UPDATED + hint
        answers = self._answers(ctx)
        if answers and answers[-1].get("event") == "done":
            return GRILL_DONE
        if answers:
            asked = ctx.latest(ctx.stage, "questions.json")
            questions = {q["id"]: q for q in json.loads(asked.read_text())["questions"]} if asked else {}
            log = ctx.run.inputs.setdefault("_grill_asked", [])
            lines = ["Answers from the human. Continue the interview per the stage contract."]
            for a in answers[-1].get("data", {}).get("answers", []):
                q = questions.get(a["id"], {})
                log.append({"text": q.get("text", ""), "answer": a["value"], "source": a["source"]})
                how = {"human": "their own answer", "default": "accepted your recommendation",
                       "skipped": "skipped: record as an open assumption"}.get(a["source"], a["source"])
                lines.append(f"- Q: {q.get('text', a['id'])}\n  A: {a['value'] if a['value'] is not None else '—'}"
                             f"  [{how}]")
            for r in ctx.run.inputs.pop("_grill_repeats", []):
                lines.append(f"- Q (you asked this again; it was already answered): {r['text']}\n  A: {r['answer']}")
            rounds = sum(1 for h in ctx.run.history if h.stage == ctx.stage and h.outcome == "needs_input")
            if bounded and cfg.get("grill.max_rounds") and rounds >= cfg.get("grill.max_rounds"):
                return "\n".join(lines) + "\n\n" + GRILL_STOP.format(
                    why=f"this project allows {cfg.get('grill.max_rounds')} rounds of questions")
            return "\n".join(lines) + hint
        incomplete = [f.get("note") for f in ctx.feedback if f.get("outcome") == "incomplete"]
        if incomplete:
            return (f"The pipeline's clarity check rejected your finish: {incomplete[-1]}. "
                    "Continue per the stage contract: ask about what is still open, or fix the checklist evidence.")
        return "Continue the interview per the stage contract." + hint

    def _split_repeats(self, ctx: StageContext, questions: list[dict]) -> tuple[list[dict], list[dict]]:
        """Questions already answered (similarity ≥ grill.dedupe_similarity) never reach a person again."""
        threshold = ctx.config.get("grill.dedupe_similarity", 0.8)
        asked = [a for a in ctx.run.inputs.get("_grill_asked", []) if a.get("text")]
        fresh, repeats = [], []
        for q in questions:
            match = max(asked, key=lambda a: similarity(a["text"], q["text"]), default=None)
            if match and similarity(match["text"], q["text"]) >= threshold:
                repeats.append({"text": q["text"], "answer": match["answer"]})
            else:
                fresh.append(q)
        return fresh, repeats

    def run(self, ctx: StageContext) -> StageResult:
        runner = _runner(ctx)
        targets = _targets(ctx)
        for t in targets:   # every repo of the feature gets its workspace; the grill explores the first
            ensure_workspace(t.ws, Path(ctx.run.inputs.get("project") or "."), runner, t.github)
        inputs = {"requirements.md": ctx.latest("a0_intake", "requirements.md"),
                  "intake.json": ctx.latest("a0_intake", "intake.json")}
        if ctx.latest("a0_intake", "context.md"):
            inputs["context.md"] = ctx.latest("a0_intake", "context.md")
        if diff := ctx.latest("a0_intake", "requirements.diff"):
            inputs["requirements.diff"] = diff
        prompt = self._prompt(ctx)
        if len(targets) > 1:
            inputs["system-map.md"] = Path(system_map(ctx, targets))
            if ctx.stage not in ctx.run.sessions:
                prompt += (" This feature spans several repos; their summaries and current specs are in "
                           "/io/in/inputs/system-map.md. Ask about cross-repo concerns too (interface contract, "
                           "rollout order, backward compatibility).")
        cfg = ctx.config
        bounded = cfg.get("grill.mode") == "bounded"
        usage: dict = {}
        for turn in range(3):    # at most two extra turns: "already answered" or "ready enough" → stop
            task = _agent_task(ctx, prompt, "grill.md", inputs, ["qa-log.md", "checklist.json"],
                               ctx.run.sessions.get(ctx.stage))
            task.workspace = targets[0].ws
            result = _run_agent(ctx, task, "io" if turn == 0 else f"io-{turn + 1}")
            usage = _add_usage(usage, result.usage)

            if result.status == "needs_input":
                raw = json.loads(result.file("questions.json").read_text())
                rounds = sum(1 for h in ctx.run.history if h.stage == ctx.stage and h.outcome == "needs_input") + 1
                questions = [{"id": f"r{rounds}-{q.get('id', i + 1)}", "text": q.get("text", ""),
                              "default": q.get("recommended") or q.get("default"), "audience": q.get("audience"),
                              "why": q.get("why")} for i, q in enumerate(raw.get("questions", []))]
                fresh, repeats = self._split_repeats(ctx, questions)
                ready = readiness(raw.get("checklist", []))
                threshold = cfg.get("grill.readiness_threshold") if bounded else None
                if not fresh or (threshold is not None and ready >= threshold):
                    why = ("every question it asked was already answered" if not fresh else
                           f"{ready:.0%} of the checklist is settled (threshold {threshold:.0%})")
                    if repeats:
                        prompt = "Already answered:\n" + "\n".join(f"- Q: {r['text']}\n  A: {r['answer']}"
                                                                     for r in repeats) + "\n\n"
                    else:
                        prompt = ""
                    prompt += GRILL_STOP.format(why=why)
                    ctx.store.log_event(ctx.run.run_id, "grill_auto_stop", reason=why)
                    continue
                cap = cfg.get("grill.questions_per_round")
                asked = fresh[:cap] if cap else fresh
                ctx.run.inputs["_grill_repeats"] = repeats
                stored = {"status": "needs_input", "questions": asked, "checklist": raw.get("checklist", []),
                          "skipped_repeats": repeats, "readiness": round(ready, 2)}
                notes = []
                if repeats:
                    notes.append(f"skipped {len(repeats)} repeated question(s)")
                warn_after = cfg.get("grill.soft_warn_rounds")
                if warn_after and rounds > warn_after:
                    notes.append(f"round {rounds}: still asking (soft warning after {warn_after} rounds)")
                return StageResult("needs_input", {"questions.json": ctx.write("questions.json", stored)},
                                   note="; ".join(notes) or None, usage=usage,
                                   gate_info={"round": rounds, "questions": asked, "readiness": round(ready, 2)})

            if result.status == "ok":
                artifacts = {name: _copy(ctx, result.file(name)) for name in ("qa-log.md", "checklist.json")}
                checklist = json.loads(result.file("checklist.json").read_text())
                problems = clarity_problems(checklist)
                threshold = cfg.get("grill.readiness_threshold") if bounded else None
                if threshold is not None and readiness(checklist.get("checklist", [])) >= threshold:
                    problems = [p for p in problems if not p.endswith("still open")]
                if problems:
                    return StageResult("incomplete", artifacts, note="; ".join(problems[:6]), usage=usage)
                return StageResult("ready", artifacts, usage=usage)

            raise SandboxError(f"grill failed: {result.failure()}")
        raise SandboxError("grill kept asking questions after being told to stop")


def _targets(ctx: StageContext) -> list[Target]:
    return plan_targets(_run_dir(ctx), ctx.run.inputs)


def system_map(ctx: StageContext, targets: list[Target]) -> str:
    """A compact map of the feature's repos for the LLM (no LLM needed to build it): role, ownership,
    dependencies, current capabilities (living specs) and the start of each CLAUDE.md."""
    existing = ctx.latest("a1_grill", "system-map.md")
    if existing:
        return str(existing)
    parts = ["# System map (repos in this feature)\n"]
    for t in targets:
        specs = sorted(p.parent.name for p in (t.ws / "openspec" / "specs").glob("*/spec.md"))
        conventions = (t.ws / "CLAUDE.md").read_text()[:1200] if (t.ws / "CLAUDE.md").is_file() else "(none)"
        parts.append(f"## {t.name}  (`{t.github}`)\n{t.info.get('description', '')}\n"
                     f"- owns: {', '.join(t.info.get('owns', []) or ['?'])}\n"
                     f"- depends on: {', '.join(t.info.get('depends_on', []) or ['nothing'])}\n"
                     f"- current capabilities (openspec/specs): {', '.join(specs) or 'none yet'}\n"
                     f"- conventions (CLAUDE.md, start):\n\n{conventions}\n")
        for spec in (t.ws / "openspec" / "specs").glob("*/spec.md"):
            parts.append(f"### {t.name} spec: {spec.parent.name}\n{spec.read_text()[:2500]}\n")
    return ctx.write("system-map.md", "\n".join(parts))


# ---- A2 propose (LLM: /to-prd then /openspec-propose, per repo in feature mode) ------------------

class Propose:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        runner = _runner(ctx)
        targets = _targets(ctx)
        for t in targets:
            ensure_workspace(t.ws, Path(ctx.run.inputs.get("project") or "."), runner, t.github)
        base_inputs = {"requirements.md": ctx.latest("a0_intake", "requirements.md"),
                       "qa-log.md": ctx.latest("a1_grill", "qa-log.md"),
                       "checklist.json": ctx.latest("a1_grill", "checklist.json")}
        if len(targets) > 1:
            base_inputs["system-map.md"] = Path(system_map(ctx, targets))
        usage: dict = {}
        artifacts: dict[str, str] = {}
        feedback = [f for f in ctx.feedback if f.get("source") in {"a3_validate", "h1_spec_review"}]
        prd = ctx.latest(ctx.stage, "prd.md")

        req_version = ctx.run.inputs.get("_req_version", 0)
        if diff := ctx.latest("a0_intake", "requirements.diff"):
            base_inputs["requirements.diff"] = diff
        if prd is not None and req_version > ctx.run.inputs.get("_prd_version", 0):   # requirement updated
            task = _agent_task(ctx, "/to-prd The requirement was updated (what changed: /io/in/inputs/requirements.diff). "
                                    "Rewrite the PRD in /io/in/inputs/prd-previous.md to match the new "
                                    "/io/in/inputs/requirements.md and the decisions in /io/in/inputs/qa-log.md. Keep "
                                    "what still holds. Follow the stage contract.",
                               "propose.md", {**base_inputs, "prd-previous.md": prd}, ["prd.md"], None)
            task.workspace = targets[0].ws
            upd = _run_agent(ctx, task, "io-prd-update")
            usage = _add_usage(usage, upd.usage)
            if upd.status != "ok":
                raise SandboxError(f"to-prd (update) failed: {upd.failure()}")
            prd = Path(_copy(ctx, upd.file("prd.md")))
            ctx.run.inputs["_prd_version"] = req_version
            feedback = feedback + [{"note": "The requirement and the PRD were updated (see requirements.diff and the "
                                            "new prd.md). Update the change(s) to match: add, change or drop slices."}]
        if prd is None:  # first attempt: one PRD for the whole requirement or feature
            ctx.run.inputs["_prd_version"] = req_version
            task = _agent_task(ctx, "/to-prd Write the PRD for the requirement in /io/in/inputs/requirements.md, using "
                                    "the clarified decisions in /io/in/inputs/qa-log.md. Follow the stage contract.",
                               "propose.md", base_inputs, ["prd.md"], None)
            task.workspace = targets[0].ws
            first = _run_agent(ctx, task, "io-prd")
            usage = _add_usage(usage, first.usage)
            if first.status != "ok":
                raise SandboxError(f"to-prd failed: {first.failure()}")
            prd = Path(_copy(ctx, first.file("prd.md")))
        artifacts["prd.md"] = str(prd)

        removed: list[str] = []
        for t in targets:
            session_key = ctx.stage if t.name is None else f"{ctx.stage}:{t.name}"
            if session_key not in ctx.run.sessions or not ctx.latest(ctx.stage, t.prefix + "changes.json"):
                prompt = ("/openspec-propose Turn the PRD in /io/in/inputs/prd.md into OpenSpec change(s), one per "
                          "vertical slice, as the stage contract describes.")
                if t.name:
                    others = ", ".join(o.name for o in targets if o is not t)
                    prompt += (f" This repo is `{t.name}` ({t.info.get('description', '')}; owns: "
                               f"{', '.join(t.info.get('owns', []))}). Plan ONLY the part of the PRD this repo owns; "
                               f"the other repos in this feature ({others}) are planned separately. If this repo "
                               "provides or consumes an interface for another repo, write the exact interface contract "
                               "(endpoints, fields, status codes, headers) in design.md under '## Contract'. If this "
                               "repo has no part in the feature, write changes.json with an empty list.")
            else:
                points = "\n".join(f"- {f.get('note') or f.get('data', {}).get('text')}" for f in feedback) or "- (none)"
                prompt = ("Revise the existing OpenSpec change(s) in /workspace/openspec/changes/ to address this "
                          f"feedback, then write /io/out/changes.json. Follow the stage contract."
                          + (f" Address only feedback that concerns repo `{t.name}`." if t.name else "")
                          + f"\nFeedback:\n{points}")
            task = _agent_task(ctx, prompt, "propose.md", {**base_inputs, "prd.md": prd}, ["changes.json"],
                               ctx.run.sessions.get(session_key))
            task.workspace = t.ws
            result = _run_agent(ctx, task, f"io-propose-{t.name}" if t.name else "io-propose", session_key)
            usage = _add_usage(usage, result.usage)
            if result.status != "ok":
                raise SandboxError(f"openspec-propose failed{f' for {t.name}' if t.name else ''}: {result.failure()}")
            artifacts[t.prefix + "changes.json"] = _copy(ctx, result.file("changes.json"), t.prefix + "changes.json")
            listed = listed_changes(result.file("changes.json"))
            changes_dir = t.ws / "openspec" / "changes"
            for change in sorted(p for p in changes_dir.iterdir() if p.is_dir() and p.name != "archive"):
                if change.name not in listed:     # changes.json is the source of truth (the agent can't rm)
                    shutil.rmtree(change)
                    removed.append(f"{t.prefix}{change.name}")
                    continue
                for f in change.rglob("*"):
                    if f.is_file() and not f.is_symlink():
                        rel = t.prefix + str(f.relative_to(t.ws))
                        artifacts[rel] = _copy(ctx, f, rel)
        return StageResult("ok", artifacts, usage=usage,
                           note=f"removed unlisted changes: {', '.join(removed)}" if removed else None)


# ---- A3 validate (OpenSpec CLI, no LLM, no network) ----------------------------------------------

class Validate:
    def __init__(self, name: str):
        self.name = name

    def run(self, ctx: StageContext) -> StageResult:
        runner = _runner(ctx)
        runner.preflight(need_gateway=False)
        targets = _targets(ctx)
        issues: list[str] = []
        changes: list[dict] = []
        validation: dict = {}
        total_listed = 0
        for t in targets:
            val = runner.run_tool(t.ws, ["openspec", "validate", "--changes", "--strict", "--json", "--no-interactive"])
            lst = runner.run_tool(t.ws, ["openspec", "list", "--json"])
            try:
                v = json.loads(val.stdout) if val.stdout.strip() else {"items": []}
                listing = json.loads(lst.stdout)
            except ValueError as exc:
                raise SandboxError(f"openspec output wasn't JSON: {val.stderr.strip()[-300:]}") from exc
            validation[t.name or "repo"] = v
            issues += [f"{t.prefix}{item['id']}: {issue.get('message', issue)}" for item in v.get("items", [])
                       for issue in item.get("issues", []) if not item.get("valid", False) or issue.get("level") == "ERROR"]
            repo_changes = [{**c, "repo": t.name} for c in listing.get("changes", [])]
            max_tasks = ctx.config.get("validate.max_tasks_per_change", 15)
            issues += [f"{t.prefix}{c['name']}: {c['totalTasks']} tasks (max {max_tasks}); split the slice"
                       for c in repo_changes if c.get("totalTasks", 0) > max_tasks]
            listed_file = ctx.latest("a2_propose", t.prefix + "changes.json")
            if listed_file:
                listed, present = listed_changes(listed_file), {c["name"] for c in repo_changes}
                total_listed += len(listed)
                issues += [f"{t.prefix}{cid}: listed in changes.json but no change folder exists"
                           for cid in sorted(listed - present)]
            changes += repo_changes
        if not changes:
            issues.append("no change folders were created")
        report = {"valid": not issues, "issues": issues, "changes": changes, "openspec": validation}
        artifacts = {"validation.json": ctx.write("validation.json", report)}
        if issues:
            return StageResult("invalid", artifacts, note="; ".join(issues[:8]))
        a2 = next((Path(h.artifacts["changes.json"]).parent for h in reversed(ctx.run.history)
                   if h.stage == "a2_propose" and "changes.json" in h.artifacts), None)
        return StageResult("ok", artifacts, gate_info={
            "changes": ", ".join(f"{c['repo'] + '/' if c.get('repo') else ''}{c['name']} ({c['totalTasks']} tasks)"
                                 for c in changes),
            "review files": str(a2) if a2 else "(none)"})
