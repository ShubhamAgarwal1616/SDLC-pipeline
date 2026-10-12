"""The engine: run stages, follow the transitions table, pause at gates, resume on events."""

from __future__ import annotations

from typing import Any, Callable

from sdlc import answers, gateway_keys, telemetry
from sdlc.config import EffectiveConfig, with_overrides
from sdlc.engine.stage import Stage, StageContext
from sdlc.engine.state import HistoryEntry, RunState, RunStatus, RunStore, Wait, new_run_id, now
from sdlc.engine.transitions import ABORT, END, ESCALATE, GATES, PIPELINES, Gate, Loop

MAX_STEPS_PER_ADVANCE = 200


class EngineError(Exception):
    """A request the run can't accept in its current state."""


def stages_of(pipeline: str) -> list[str]:
    seen: list[str] = []
    for stage, _ in PIPELINES[pipeline].transitions:
        if stage not in seen:
            seen.append(stage)
    return seen


class Engine:
    def __init__(self, store: RunStore | None = None,
                 stage_lookup: Callable[..., Stage] | None = None):
        from sdlc.stages import get_stage  # deferred: stages import the engine's types

        self.store = store or RunStore()
        self.stage_lookup = stage_lookup or get_stage

    # ---- lifecycle ---------------------------------------------------------------

    def start(self, pipeline: str, config: EffectiveConfig, inputs: dict[str, Any],
              fake: bool = False, fake_failures: dict[str, int] | None = None,
              feature_id: str | None = None, fake_stages: list[str] | None = None) -> RunState:
        if pipeline not in PIPELINES:
            raise EngineError(f"unknown pipeline {pipeline!r}")
        state = RunState(run_id=new_run_id(), pipeline=pipeline, current=PIPELINES[pipeline].start,
                         inputs=inputs, fake=fake, fake_failures=fake_failures or {},
                         feature_id=feature_id, fake_stages=fake_stages or [])
        run_dir = self.store.run_dir(state.run_id)
        run_dir.mkdir(parents=True, exist_ok=True)
        (run_dir / "config.json").write_text(config.to_json())  # frozen for the life of the run
        self.store.save(state)
        self.store.log_event(state.run_id, "run_started", pipeline=pipeline, fake=fake, inputs=inputs)
        return self.advance(state)

    def config_for(self, state: RunState) -> EffectiveConfig:
        return EffectiveConfig.from_json((self.store.run_dir(state.run_id) / "config.json").read_text())

    def advance(self, state: RunState) -> RunState:
        cfg = self.config_for(state)
        pipeline = PIPELINES[state.pipeline]
        steps = 0
        while state.status == RunStatus.RUNNING:
            steps += 1
            if steps > MAX_STEPS_PER_ADVANCE:
                self._escalate(state, state.current, "error", "step limit reached (possible unbounded loop)")
                break
            name = state.current
            attempt = state.attempts.get(name, 0) + 1
            state.attempts[name] = attempt
            feedback = state.feedback.pop(name, [])
            ctx = StageContext(run=state, config=cfg, store=self.store, stage=name,
                               attempt=attempt, feedback=feedback)
            started = now()
            try:
                result = self.stage_lookup(name, fake=state.fake or name in state.fake_stages).run(ctx)
            except Exception as exc:  # a crashing stage escalates; it never kills the run record
                state.history.append(HistoryEntry(stage=name, attempt=attempt, outcome="error",
                                                  started=started, finished=now(), note=str(exc)))
                if feedback:   # keep it, so `sdlc resume` re-applies the same feedback
                    state.feedback.setdefault(name, [])[:0] = feedback
                self._escalate(state, name, "error", f"{name} raised {type(exc).__name__}: {exc}")
                self.store.save(state)
                break
            state.history.append(HistoryEntry(stage=name, attempt=attempt, outcome=result.outcome,
                                              started=started, finished=now(), artifacts=result.artifacts,
                                              usage=result.usage, note=result.note))
            try:
                telemetry.record(self.store.run_dir(state.run_id), state.run_id, state.pipeline, state.history[-1])
            except Exception:
                pass
            self.store.log_event(state.run_id, "stage_finished", stage=name, attempt=attempt,
                                 outcome=result.outcome)
            nxt = pipeline.transitions.get((name, result.outcome))
            if nxt is None:
                self._escalate(state, name, result.outcome, f"no transition for ({name}, {result.outcome})")
            else:
                feedback = {"source": name, "outcome": result.outcome, "note": result.note,
                            "artifacts": result.artifacts}
                self._go(state, cfg, nxt, origin=name, outcome=result.outcome,
                         feedback=feedback, gate_info=result.gate_info)
            self.store.save(state)
        return state

    # ---- human events --------------------------------------------------------------

    def deliver(self, run_id: str, event: str, payload: dict[str, Any] | None = None,
                event_id: str | None = None) -> RunState:
        state = self.store.load(run_id)
        if state.status != RunStatus.WAITING_HUMAN or state.wait is None:
            raise EngineError(f"{run_id} is {state.status.value}, not waiting for a human")
        if event_id and event_id in state.seen_event_ids:
            self.store.log_event(run_id, "event_duplicate_ignored", event=event, event_id=event_id)
            return state
        if event not in state.wait.wake_on:
            raise EngineError(f"gate {state.wait.gate} accepts {state.wait.wake_on}, not {event!r}")
        gate = GATES[state.wait.gate]
        if event_id:
            state.seen_event_ids.append(event_id)
        self.store.log_event(run_id, "human_event", gate=gate.name, event=event, payload=payload or {})
        state.status, state.wait = RunStatus.RUNNING, None
        self._go(state, self.config_for(state), gate.events[event], origin=gate.name, outcome=event,
                 feedback={"source": gate.name, "event": event, "data": payload or {}}, carry=True)
        self.store.save(state)
        return self.advance(state)

    def resume(self, run_id: str, from_stage: str | None = None,
               overrides: dict[str, Any] | None = None) -> RunState:
        state = self.store.load(run_id)
        if overrides:
            cfg = with_overrides(self.config_for(state), overrides)
            (self.store.run_dir(run_id) / "config.json").write_text(cfg.to_json())
            self.store.log_event(run_id, "config_changed_on_resume", overrides=overrides)
        if state.status in (RunStatus.DONE, RunStatus.ABORTED):
            raise EngineError(f"{run_id} is {state.status.value}; start a new run instead")
        if state.status == RunStatus.WAITING_HUMAN and not from_stage:
            raise EngineError(f"{run_id} is waiting at {state.wait.gate}; answer or review it, "
                              "or pass --from to restart a stage")
        if from_stage:
            if from_stage not in stages_of(state.pipeline):
                raise EngineError(f"{from_stage!r} is not a stage of {state.pipeline}: {stages_of(state.pipeline)}")
            state.current = from_stage
            for key in [k for k in state.loops if k.endswith(f"->{from_stage}")]:
                del state.loops[key]
        from sdlc.stages import REAL_STAGES   # stages built since the run started now run for real
        upgraded = [s for s in state.fake_stages if s in REAL_STAGES]
        state.fake_stages = [s for s in state.fake_stages if s not in REAL_STAGES]
        state.status, state.wait, state.escalation = RunStatus.RUNNING, None, None
        self.store.log_event(run_id, "resumed", from_stage=state.current, now_real=upgraded)
        self.store.save(state)
        return self.advance(state)

    def update_requirements(self, run_id: str, new_file) -> RunState:
        """Replace a plan run's requirement (PLAN.md §21: `sdlc plan --run RUN --update FILE`). Intake diffs it against
        the previous version, the grill asks only about what changed, and the PRD and changes are revised."""
        import shutil
        from pathlib import Path
        state = self.store.load(run_id)
        if state.pipeline != "plan":
            raise EngineError(f"{run_id} is a {state.pipeline} run; only plan runs take a new requirement")
        if state.status in (RunStatus.DONE, RunStatus.ABORTED, RunStatus.RUNNING):
            raise EngineError(f"{run_id} is {state.status.value}: " + (
                "its stories are published; start a new run" if state.status == RunStatus.DONE else "can't update now"))
        version = state.inputs.get("_req_version", 0) + 1
        dest = self.store.run_dir(run_id) / f"requirements-v{version}.md"
        shutil.copyfile(Path(new_file), dest)
        state.inputs["requirements"], state.inputs["_req_version"] = str(dest), version
        state.status, state.wait, state.escalation = RunStatus.RUNNING, None, None
        state.current, state.loops = PIPELINES[state.pipeline].start, {}
        self.store.log_event(run_id, "requirements_updated", version=version)
        self.store.save(state)
        return self.advance(state)

    def abort(self, run_id: str, reason: str = "") -> RunState:
        state = self.store.load(run_id)
        if state.status in (RunStatus.DONE, RunStatus.ABORTED):
            raise EngineError(f"{run_id} is already {state.status.value}")
        state.status, state.wait = RunStatus.ABORTED, None
        self.store.log_event(run_id, "aborted", reason=reason)
        self._finish(state)
        self.store.save(state)
        return state

    # ---- transitions ------------------------------------------------------------------

    def _go(self, state: RunState, cfg: EffectiveConfig, nxt: Any, origin: str, outcome: str | None = None,
            feedback: dict | None = None, gate_info: dict | None = None, carry: bool = False,
            reason: str | None = None) -> None:
        if isinstance(nxt, Gate):
            if nxt.enabled and not cfg.get(nxt.enabled, False):
                self.store.log_event(state.run_id, "gate_skipped", gate=nxt.name, because=f"{nxt.enabled}=false")
                return self._go(state, cfg, nxt.skip_to, origin=nxt.name)
            state.status, state.current = RunStatus.WAITING_HUMAN, nxt.name
            state.wait = Wait(gate=nxt.name, since=now(), wake_on=sorted(nxt.events), info=gate_info or {})
            if questions := (gate_info or {}).get("questions"):
                path = self.store.run_dir(state.run_id) / "answers.md"
                path.write_text(answers.render(state.run_id, (gate_info or {}).get("round", 1), questions))
            self.store.log_event(state.run_id, "waiting_human", gate=nxt.name)
            return None
        if isinstance(nxt, Loop):
            key = f"{origin}->{nxt.to}"
            limit = cfg.get(nxt.limit) if nxt.limit else None
            used = state.loops.get(key, 0)
            if limit is not None and used >= limit:
                return self._go(state, cfg, nxt.then, origin=origin, outcome=outcome,
                                reason=f"loop {key} reached its limit ({nxt.limit}={limit})")
            state.loops[key] = used + 1
            if feedback:
                state.feedback.setdefault(nxt.to, []).append(feedback)
            state.current = nxt.to
            return None
        if nxt == END:
            state.status, state.current = RunStatus.DONE, None
            self.store.log_event(state.run_id, "run_done")
            self._finish(state)
        elif nxt == ESCALATE:
            note = (feedback or {}).get("note")
            self._escalate(state, origin, outcome,
                           reason or f"{origin} ended with {outcome!r}" + (f": {note}" if note else ""))
        elif nxt == ABORT:
            state.status, state.current = RunStatus.ABORTED, None
            self.store.log_event(state.run_id, "aborted", reason=f"{origin}: {outcome}")
            self._finish(state)
        else:
            # A forward move ends any retry loop that started at this stage.
            for key in [k for k in state.loops if k.startswith(f"{origin}->")]:
                del state.loops[key]
            if carry and feedback:
                state.feedback.setdefault(nxt, []).append(feedback)
            state.current = nxt
        return None

    def _finish(self, state: RunState) -> None:
        """A finished run's gateway keys stop working."""
        if not state.fake:
            revoked = gateway_keys.revoke_run(state.run_id)
            if revoked:
                self.store.log_event(state.run_id, "gateway_keys_revoked", count=revoked)

    def _escalate(self, state: RunState, at: str | None, outcome: str | None, reason: str) -> None:
        state.status = RunStatus.ESCALATED
        state.escalation = {"at": at, "outcome": outcome, "reason": reason, "since": now()}
        self.store.log_event(state.run_id, "escalated", at=at, reason=reason)
