"""Per-stage trace spans (PLAN.md §6, P7).

Every stage execution becomes one span in runs/<id>/spans.jsonl (trace id = the run). If
OTEL_EXPORTER_OTLP_ENDPOINT is set (env or .env), spans are also sent best-effort as OTLP/HTTP JSON
to <endpoint>/v1/traces, so any OpenTelemetry collector (Jaeger, Tempo, Langfuse, Honeycomb…) can show them.
No extra dependencies.
"""

from __future__ import annotations

import hashlib
import json
import os
import urllib.request
from datetime import datetime
from pathlib import Path

from sdlc import secrets


def _ns(iso: str) -> int:
    return int(datetime.fromisoformat(iso).timestamp() * 1_000_000_000)


def trace_id(run_id: str) -> str:
    return hashlib.sha256(run_id.encode()).hexdigest()[:32]


def span_for(run_id: str, pipeline: str, entry) -> dict:
    usage = entry.usage or {}
    return {
        "trace_id": trace_id(run_id),
        "span_id": hashlib.sha256(f"{run_id}/{entry.stage}/{entry.attempt}/{entry.started}".encode()).hexdigest()[:16],
        "name": entry.stage,
        "start_ns": _ns(entry.started), "end_ns": _ns(entry.finished),
        "attributes": {"sdlc.run_id": run_id, "sdlc.pipeline": pipeline, "sdlc.stage": entry.stage,
                       "sdlc.attempt": entry.attempt, "sdlc.outcome": entry.outcome,
                       "llm.cost_usd": usage.get("cost_usd", 0.0), "llm.calls": usage.get("calls", 0),
                       "llm.input_tokens": usage.get("input_tokens", 0), "llm.output_tokens": usage.get("output_tokens", 0),
                       "llm.cache_read_tokens": usage.get("cache_read_tokens", 0),
                       "llm.models": ",".join(usage.get("models", []) or [])},
        "status": "error" if entry.outcome in {"error", "blocked"} else "ok",
    }


def _otlp(span: dict) -> dict:
    def attr(k, v):
        if isinstance(v, bool):
            return {"key": k, "value": {"boolValue": v}}
        if isinstance(v, int):
            return {"key": k, "value": {"intValue": str(v)}}
        if isinstance(v, float):
            return {"key": k, "value": {"doubleValue": v}}
        return {"key": k, "value": {"stringValue": str(v)}}
    return {"resourceSpans": [{
        "resource": {"attributes": [attr("service.name", "sdlc-pipeline")]},
        "scopeSpans": [{"scope": {"name": "sdlc.engine"}, "spans": [{
            "traceId": span["trace_id"], "spanId": span["span_id"], "name": span["name"], "kind": 1,
            "startTimeUnixNano": str(span["start_ns"]), "endTimeUnixNano": str(span["end_ns"]),
            "attributes": [attr(k, v) for k, v in span["attributes"].items()],
            "status": {"code": 2 if span["status"] == "error" else 1}}]}]}]}


def record(run_dir: Path, run_id: str, pipeline: str, entry) -> dict:
    span = span_for(run_id, pipeline, entry)
    with (run_dir / "spans.jsonl").open("a") as fh:
        fh.write(json.dumps(span) + "\n")
    endpoint = os.environ.get("OTEL_EXPORTER_OTLP_ENDPOINT") or secrets.get("OTEL_EXPORTER_OTLP_ENDPOINT", required=False)
    if endpoint:
        try:
            req = urllib.request.Request(endpoint.rstrip("/") + "/v1/traces", data=json.dumps(_otlp(span)).encode(),
                                         headers={"Content-Type": "application/json"}, method="POST")
            urllib.request.urlopen(req, timeout=3).close()
        except Exception:
            pass   # telemetry must never break a run
    return span
