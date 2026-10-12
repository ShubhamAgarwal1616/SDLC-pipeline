"""Re-run the A4 publish stage against an existing run's change folders (P4 testing aid).

    uv run python scripts/publish_from_run.py <run-id> [--dry-run]
Uses the run's frozen config, overriding the project to Jira KAN. Safe to repeat: issues are found by label.
"""
import json
import sys

from sdlc.config import with_overrides
from sdlc.engine import RunStore
from sdlc.engine.runner import Engine
from sdlc.engine.stage import StageContext
from sdlc.stages.publish import Publish

run_id, dry = sys.argv[1], "--dry-run" in sys.argv
store = RunStore()
state = store.load(run_id)
state.inputs["dry_run"] = dry
state.inputs["repo"] = None
cfg = with_overrides(Engine(store).config_for(state), {"project.jira.project_key": "KAN"}, "publish script")
ctx = StageContext(run=state, config=cfg, store=store, stage="a4_publish", attempt=90 + (0 if dry else 1), feedback=[])
result = Publish("a4_publish").run(ctx)
print(result.outcome, "|", result.note)
for name, path in result.artifacts.items():
    print(f"--- {name}\n{open(path).read()}")
