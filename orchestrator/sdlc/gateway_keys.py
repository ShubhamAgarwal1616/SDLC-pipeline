"""Per-run, per-stage gateway keys and the gateway's usage log (PLAN.md §25.1).

The orchestrator writes keys.json; the gateway reads it on every call. A key carries the
stage budget, so the gateway itself stops a stage that overspends. Keys are revoked when a
run finishes. Usage is read back from usage.jsonl to fill `sdlc report`.
"""

from __future__ import annotations

import json
import os
import secrets
from pathlib import Path

from sdlc.config import RUNS


def data_dir() -> Path:
    return Path(os.environ.get("SDLC_GATEWAY_DATA", RUNS / "_gateway"))


def _keys_file() -> Path:
    return data_dir() / "keys.json"


def _usage_file() -> Path:
    return data_dir() / "usage.jsonl"


def _load() -> dict:
    path = _keys_file()
    return json.loads(path.read_text()) if path.exists() else {}


def _save(keys: dict) -> None:
    path = _keys_file()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(keys, indent=2))
    tmp.replace(path)


def key_for(run_id: str, stage: str, budget_usd: float | None) -> str:
    """Reuse the run+stage key across attempts, so its budget covers every retry of the stage."""
    keys = _load()
    for token, entry in keys.items():
        if entry.get("run_id") == run_id and entry.get("stage") == stage:
            if entry.get("budget_usd") != budget_usd:   # budget raised (or lowered) via resume --set
                entry["budget_usd"] = budget_usd
                _save(keys)
            return token
    token = "sdlc-" + secrets.token_urlsafe(24)
    keys[token] = {"run_id": run_id, "stage": stage, "budget_usd": budget_usd}
    _save(keys)
    return token


def revoke_run(run_id: str) -> int:
    keys = _load()
    kept = {t: e for t, e in keys.items() if e.get("run_id") != run_id}
    if len(kept) != len(keys):
        _save(kept)
    return len(keys) - len(kept)


def usage_offset() -> int:
    path = _usage_file()
    return path.stat().st_size if path.exists() else 0


def _records(offset: int = 0) -> list[dict]:
    path = _usage_file()
    if not path.exists():
        return []
    with path.open() as fh:
        fh.seek(offset)
        return [json.loads(line) for line in fh.read().splitlines() if line.strip()]


def usage_since(offset: int, run_id: str, stage: str) -> dict:
    recs = [r for r in _records(offset) if r.get("run_id") == run_id and r.get("stage") == stage]
    return {
        "calls": len(recs),
        "input_tokens": sum(r.get("input_tokens", 0) for r in recs),
        "output_tokens": sum(r.get("output_tokens", 0) for r in recs),
        "cache_read_tokens": sum(r.get("cache_read_tokens", 0) for r in recs),
        "cache_write_tokens": sum(r.get("cache_write_tokens", 0) for r in recs),
        "cost_usd": round(sum(r.get("cost_usd", 0.0) for r in recs), 6),
        "models": sorted({r.get("upstream_model") for r in recs if r.get("upstream_model")}),
    }


def stage_spent(run_id: str, stage: str) -> float:
    return round(sum(r.get("cost_usd", 0.0) for r in _records()
                     if r.get("run_id") == run_id and r.get("stage") == stage), 6)


def all_records() -> list[dict]:
    return _records()


def run_spent(run_id: str) -> float:
    return round(sum(r.get("cost_usd", 0.0) for r in _records() if r.get("run_id") == run_id), 6)
