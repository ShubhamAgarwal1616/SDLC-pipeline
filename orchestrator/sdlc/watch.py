"""`sdlc watch`: poll GitHub for events on runs waiting at a PR gate and resume them (PLAN.md §19).

Local mode can't receive webhooks, so it polls. Each event goes through the same checks a webhook
would: it must belong to a waiting run, come from a trusted author, be newer than the gate, and
not have been seen before (event ids are de-duplicated by the engine).
"""

from __future__ import annotations

from datetime import datetime, timezone

from sdlc.connectors import github
from sdlc.engine import Engine, EngineError, RunStatus
from sdlc.engine.state import now

PR_GATES = {"h1_spec_review", "h3_pr_review", "h4_merge"}
# per gate: which /sdlc command means "send my comments back"
FEEDBACK_COMMAND = {"h1_spec_review": "/sdlc revise", "h3_pr_review": "/sdlc fix"}


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def feedback_text(items: list[dict], command: dict, prefix: str = "/sdlc revise") -> str:
    lines = []
    extra = command["body"].strip()[len(prefix):].strip()
    if extra:
        where = f"{command['path']}:{command.get('line') or '?'} " if command.get("path") else ""
        lines.append(f"- {where}{extra}")
    for c in items:
        where = f"{c['path']}:{c.get('line') or '?'} " if c.get("path") else ""
        lines.append(f"- {where}({c['author']}) {c['body'].strip()}")
    return "\n".join(lines)


def nudge_waiting(engine: Engine) -> list[str]:
    """Reminders (PR comment, or an event for CLI gates) at human_wait.reminders_h; stale after N days."""
    actions = []
    for run in engine.store.list():
        if run.status != RunStatus.WAITING_HUMAN or not run.wait:
            continue
        cfg = engine.config_for(run)
        age_h = (datetime.now(timezone.utc) - _ts(run.wait.since)).total_seconds() / 3600
        stale_days = cfg.get("human_wait.stale_after_days")
        if stale_days and age_h >= stale_days * 24:
            run.status = RunStatus.ESCALATED
            run.escalation = {"at": run.wait.gate, "outcome": "stale", "since": now(),
                              "reason": f"stale: no response at {run.wait.gate} for {int(age_h // 24)} days "
                                        "(resume to keep waiting, or abort)"}
            engine.store.log_event(run.run_id, "stale", gate=run.wait.gate)
            engine.store.save(run)
            actions.append(f"{run.run_id}: stale after {int(age_h // 24)} days → escalated")
            continue
        sent = run.wait.info.setdefault("reminders_sent", [])
        due = [h for h in cfg.get("human_wait.reminders_h", []) or [] if age_h >= h and h not in sent]
        if not due:
            continue
        hours = max(due)
        repo, number = run.wait.info.get("repo"), run.wait.info.get("pr_number")
        if repo and number:
            github.comment(repo, number, f"Reminder: this pipeline run (`{run.run_id}`) has been waiting for review "
                                         f"for {int(age_h)}h. {run.wait.info.get('how', '')}")
        sent.extend(due)
        engine.store.log_event(run.run_id, "reminder", gate=run.wait.gate, hours=hours)
        engine.store.save(run)
        actions.append(f"{run.run_id}: reminder at {run.wait.gate} ({int(age_h)}h waiting)")
    return actions


def _gate_prs(info: dict) -> list[dict]:
    """A gate waits on one PR ({repo, pr_number}) or, for a multi-repo feature, several ({prs: [...]})."""
    if info.get("prs"):
        return [{"repo": p["repo"], "number": p["pr_number"], "name": p.get("name") or p["repo"]} for p in info["prs"]]
    if info.get("repo") and info.get("pr_number"):
        return [{"repo": info["repo"], "number": info["pr_number"], "name": info["repo"]}]
    return []


def poll_once(engine: Engine) -> list[str]:
    actions = nudge_waiting(engine)
    for run in engine.store.list():
        if run.status != RunStatus.WAITING_HUMAN or not run.wait or run.wait.gate not in PR_GATES:
            continue
        refs = _gate_prs(run.wait.info)
        if not refs:
            continue
        gate = run.wait.gate
        try:
            states = [(ref, github.get_pr(ref["repo"], ref["number"])) for ref in refs]
            label = ", ".join(f"{r['name']}#{r['number']}" for r in refs)
            if all(pr.get("merged") for _, pr in states):
                event = "merged" if gate == "h4_merge" else "approved"
                engine.deliver(run.run_id, event, {"merged": True, "prs": label},
                               event_id=f"merged-{gate}-" + "-".join(f"{r['repo']}#{r['number']}" for r in refs))
                actions.append(f"{run.run_id}: {label} merged → {event}")
                continue
            closed = [ref for ref, pr in states if pr.get("state") == "closed" and not pr.get("merged")]
            if closed:
                engine.deliver(run.run_id, "abort", {"reason": f"PR closed without merge: {closed[0]['name']}"},
                               event_id=f"closed-{closed[0]['repo']}#{closed[0]['number']}")
                actions.append(f"{run.run_id}: {closed[0]['name']}#{closed[0]['number']} closed → aborted")
                continue
            since = _ts(run.wait.since)
            new = []
            for ref in refs:   # comments from every PR of the gate, tagged with their repo in multi-PR gates
                for c in github.conversation(ref["repo"], ref["number"]):
                    if c["at"] and _ts(c["at"]) > since:
                        if len(refs) > 1 and c.get("path"):
                            c = {**c, "path": f"[{ref['name']}] {c['path']}"}
                        elif len(refs) > 1:
                            c = {**c, "body": c["body"] if c["body"].strip().startswith("/sdlc")
                                 else f"[{ref['name']}] {c['body']}"}
                        new.append(c)
            new.sort(key=lambda c: c["at"])
            # a command counts wherever it's written: PR comment, line comment or review summary
            commands = [c for c in new if c["body"].strip().startswith("/sdlc")
                        and c.get("association") in github.TRUSTED_ASSOCIATIONS]
            if not commands:
                continue
            cmd = commands[-1]
            approved_review = gate == "h3_pr_review" and any(
                c["kind"] == "review" and c.get("state") == "APPROVED" for c in new)
            if gate == "h3_pr_review" and (cmd["body"].strip().startswith("/sdlc approve") or approved_review):
                engine.deliver(run.run_id, "approved", {"by": cmd["author"]}, event_id=cmd["id"])
                actions.append(f"{run.run_id}: /sdlc approve → archiving")
                continue
            if cmd["body"].strip().startswith("/sdlc abort"):
                engine.deliver(run.run_id, "abort", {"reason": f"/sdlc abort by {cmd['author']}"}, event_id=cmd["id"])
                actions.append(f"{run.run_id}: /sdlc abort → aborted")
            elif (wanted := FEEDBACK_COMMAND.get(gate)) and cmd["body"].strip().startswith(wanted):
                notes = [c for c in new if not c["body"].strip().startswith("/sdlc")]
                text = feedback_text(notes, cmd, wanted)
                engine.deliver(run.run_id, "changes", {"text": text, "comments": notes}, event_id=cmd["id"])
                actions.append(f"{run.run_id}: {wanted} with {len(notes)} comment(s) → revising")
        except EngineError as exc:
            actions.append(f"{run.run_id}: skipped ({exc})")
    try:
        from sdlc.feature import advance_all
        actions += advance_all(engine)
    except ImportError:
        pass
    return actions
