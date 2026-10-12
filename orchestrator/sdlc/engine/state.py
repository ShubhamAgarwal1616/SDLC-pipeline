"""Run state and its on-disk store: runs/<run_id>/{state.json, config.json, events.jsonl, artifacts/}."""

from __future__ import annotations

import json
import os
import secrets
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from sdlc.config import RUNS


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class RunStatus(StrEnum):
    RUNNING = "running"
    WAITING_HUMAN = "waiting_human"
    ESCALATED = "escalated"
    DONE = "done"
    ABORTED = "aborted"


class Wait(BaseModel):
    gate: str
    since: str
    wake_on: list[str]
    info: dict[str, Any] = Field(default_factory=dict)


class HistoryEntry(BaseModel):
    stage: str
    attempt: int
    outcome: str
    started: str
    finished: str
    artifacts: dict[str, str] = Field(default_factory=dict)
    usage: dict[str, Any] = Field(default_factory=dict)
    note: str | None = None


class RunState(BaseModel):
    run_id: str
    pipeline: str
    status: RunStatus = RunStatus.RUNNING
    current: str | None = None
    created: str = Field(default_factory=now)
    updated: str = Field(default_factory=now)
    inputs: dict[str, Any] = Field(default_factory=dict)
    feature_id: str | None = None
    fake: bool = False
    fake_stages: list[str] = Field(default_factory=list)   # stages faked because not built yet
    fake_failures: dict[str, int] = Field(default_factory=dict)
    sessions: dict[str, str] = Field(default_factory=dict)   # stage → Claude Code session id (resume)
    attempts: dict[str, int] = Field(default_factory=dict)
    loops: dict[str, int] = Field(default_factory=dict)
    feedback: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    wait: Wait | None = None
    escalation: dict[str, Any] | None = None
    seen_event_ids: list[str] = Field(default_factory=list)
    history: list[HistoryEntry] = Field(default_factory=list)


def new_run_id() -> str:
    return f"run-{datetime.now().strftime('%Y%m%d-%H%M%S')}-{secrets.token_hex(2)}"


class RunStore:
    def __init__(self, root: Path | None = None):
        self.root = Path(root or os.environ.get("SDLC_RUNS_DIR", RUNS))

    def run_dir(self, run_id: str) -> Path:
        return self.root / run_id

    def exists(self, run_id: str) -> bool:
        return (self.run_dir(run_id) / "state.json").exists()

    def save(self, state: RunState) -> None:
        state.updated = now()
        path = self.run_dir(state.run_id) / "state.json"
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(state.model_dump_json(indent=2))
        tmp.replace(path)  # atomic: a crash never leaves a half-written state

    def load(self, run_id: str) -> RunState:
        path = self.run_dir(run_id) / "state.json"
        if not path.exists():
            raise KeyError(f"no run {run_id!r} in {self.root}")
        return RunState.model_validate_json(path.read_text())

    def list(self) -> list[RunState]:
        if not self.root.exists():
            return []
        runs = [self.load(p.name) for p in self.root.iterdir() if (p / "state.json").exists()]
        return sorted(runs, key=lambda r: r.created, reverse=True)

    def artifacts_dir(self, run_id: str, stage: str, attempt: int) -> Path:
        path = self.run_dir(run_id) / "artifacts" / stage / f"attempt-{attempt}"
        path.mkdir(parents=True, exist_ok=True)
        return path

    def log_event(self, run_id: str, kind: str, **data: Any) -> None:
        path = self.run_dir(run_id) / "events.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            fh.write(json.dumps({"at": now(), "kind": kind, **data}) + "\n")
