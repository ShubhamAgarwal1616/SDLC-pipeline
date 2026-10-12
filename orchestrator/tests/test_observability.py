import json

from sdlc import reporting, telemetry
from sdlc.engine.state import HistoryEntry, RunState, RunStatus, RunStore


def test_spans_and_report(tmp_path, monkeypatch):
    monkeypatch.setenv("SDLC_GATEWAY_DATA", str(tmp_path / "gw"))
    monkeypatch.delenv("OTEL_EXPORTER_OTLP_ENDPOINT", raising=False)
    monkeypatch.setattr("sdlc.secrets.get", lambda name, required=True: None)
    store = RunStore(tmp_path / "runs")
    h = [HistoryEntry(stage="b3_implement", attempt=1, outcome="ok", started="2026-10-12T10:00:00+00:00",
                      finished="2026-10-12T10:05:00+00:00", usage={"cost_usd": 0.5, "calls": 3, "models": ["m"]}),
         HistoryEntry(stage="b4_verify", attempt=1, outcome="fail", started="2026-10-12T10:05:00+00:00",
                      finished="2026-10-12T10:06:00+00:00")]
    state = RunState(run_id="run-x", pipeline="implement", status=RunStatus.DONE, inputs={"ticket": "KAN-1"}, history=h)
    store.save(state)
    span = telemetry.record(store.run_dir("run-x"), "run-x", "implement", h[0])
    assert span["trace_id"] == telemetry.trace_id("run-x") and len(span["span_id"]) == 16
    assert span["attributes"]["llm.cost_usd"] == 0.5
    assert json.loads((store.run_dir("run-x") / "spans.jsonl").read_text().splitlines()[0])["name"] == "b3_implement"
    otlp = telemetry._otlp(span)
    assert otlp["resourceSpans"][0]["scopeSpans"][0]["spans"][0]["name"] == "b3_implement"

    (tmp_path / "gw").mkdir()
    (tmp_path / "gw" / "usage.jsonl").write_text("\n".join(json.dumps(r) for r in [
        {"run_id": "run-x", "stage": "b3_implement", "upstream_model": "m", "cost_usd": 0.5, "input_tokens": 10,
         "cache_read_tokens": 90, "cache_write_tokens": 0, "status": 200},
        {"run_id": "run-x", "stage": "b5_review", "upstream_model": "o", "cost_usd": 0.25, "input_tokens": 50,
         "cache_read_tokens": 50, "cache_write_tokens": 0, "status": 403}]) + "\n")
    data = reporting.collect(store)
    assert data["totals"]["cost"] == 0.75 and data["totals"]["errors"] == 1 and data["totals"]["cache_hit"] == 0.7
    assert data["runs"][0]["loops"] == 1 and data["runs"][0]["subject"] == "KAN-1"
    assert data["stages"]["b3_implement"]["cache_hit"] == 0.9
    html = reporting.dashboard_html(data)
    assert "<svg" in html and "run-x" in html and "prefers-color-scheme" in html
