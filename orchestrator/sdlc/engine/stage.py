"""The stage interface every pipeline step implements (scripts, LLM stages, fakes)."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from sdlc.config import EffectiveConfig
from sdlc.engine.state import RunState, RunStore


@dataclass
class StageResult:
    outcome: str                       # key into the transitions table, e.g. "ok", "fail", "needs_input"
    artifacts: dict[str, str] = field(default_factory=dict)
    note: str | None = None            # short text carried as feedback if this outcome loops
    usage: dict[str, Any] = field(default_factory=dict)
    gate_info: dict[str, Any] = field(default_factory=dict)  # shown to the human at the next gate


@dataclass
class StageContext:
    run: RunState
    config: EffectiveConfig
    store: RunStore
    stage: str
    attempt: int
    feedback: list[dict[str, Any]]

    @property
    def out_dir(self) -> Path:
        return self.store.artifacts_dir(self.run.run_id, self.stage, self.attempt)

    def write(self, name: str, content: Any) -> str:
        path = self.out_dir / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content if isinstance(content, str) else json.dumps(content, indent=2))
        return str(path)

    def latest(self, stage: str, name: str) -> Path | None:
        """Most recent artifact `name` produced by `stage` in this run."""
        for entry in reversed(self.run.history):
            if entry.stage == stage and name in entry.artifacts:
                return Path(entry.artifacts[name])
        return None


class Stage(Protocol):
    name: str

    def run(self, ctx: StageContext) -> StageResult: ...
