"""Cross-run cost and quality reporting (P7): `sdlc report --all` and `sdlc dashboard`.

Data sources: run state (runs/<id>/state.json) and the gateway's usage log (runs/_gateway/usage.jsonl).
"""

from __future__ import annotations

import html
import json
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from sdlc import gateway_keys
from sdlc.engine.state import RunStore


def _secs(a: str, b: str) -> float:
    return (datetime.fromisoformat(b) - datetime.fromisoformat(a)).total_seconds()


def cache_hit(rec: dict) -> float:
    total = rec.get("input_tokens", 0) + rec.get("cache_read_tokens", 0) + rec.get("cache_write_tokens", 0)
    return rec.get("cache_read_tokens", 0) / total if total else 0.0


def collect(store: RunStore | None = None) -> dict:
    store = store or RunStore()
    usage = gateway_keys.all_records()
    by_run: dict[str, list[dict]] = defaultdict(list)
    for rec in usage:
        by_run[rec.get("run_id")].append(rec)
    runs = []
    for run in store.list():
        if run.fake:
            continue
        recs = by_run.get(run.run_id, [])
        first = run.history[0].started if run.history else run.created
        last = run.history[-1].finished if run.history else run.updated
        runs.append({
            "run_id": run.run_id, "pipeline": run.pipeline, "status": run.status.value,
            "subject": run.inputs.get("ticket") or Path(run.inputs.get("requirements") or "").name,
            "cost": round(sum(r.get("cost_usd", 0) for r in recs), 4), "calls": len(recs),
            "stage_runs": len(run.history),
            "loops": sum(1 for h in run.history if h.outcome in {"fail", "blocking", "invalid", "incomplete"}),
            "escalations": sum(1 for h in run.history if h.outcome in {"error", "blocked"}),
            "elapsed_min": round(_secs(first, last) / 60, 1),
            "human_wait_min": round(sum(_secs(a.finished, b.started) for a, b in zip(run.history, run.history[1:])
                                        if _secs(a.finished, b.started) > 30) / 60, 1),
        })
    stages: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0, "in": 0, "out": 0, "cache_read": 0, "cache_write": 0})
    models: dict[str, dict] = defaultdict(lambda: {"calls": 0, "cost": 0.0})
    for rec in usage:
        s = stages[rec.get("stage") or "?"]
        s["calls"] += 1
        s["cost"] += rec.get("cost_usd", 0)
        s["in"] += rec.get("input_tokens", 0)
        s["out"] += rec.get("output_tokens", 0)
        s["cache_read"] += rec.get("cache_read_tokens", 0)
        s["cache_write"] += rec.get("cache_write_tokens", 0)
        m = models[rec.get("upstream_model") or "?"]
        m["calls"] += 1
        m["cost"] += rec.get("cost_usd", 0)
    for s in stages.values():
        total = s["in"] + s["cache_read"] + s["cache_write"]
        s["cache_hit"] = round(s["cache_read"] / total, 3) if total else 0.0
        s["cost"] = round(s["cost"], 4)
    total_in = sum(r.get("input_tokens", 0) + r.get("cache_read_tokens", 0) + r.get("cache_write_tokens", 0) for r in usage)
    return {
        "runs": runs, "stages": dict(stages), "models": {k: {**v, "cost": round(v["cost"], 4)} for k, v in models.items()},
        "totals": {"cost": round(sum(r.get("cost_usd", 0) for r in usage), 4), "calls": len(usage),
                   "runs": len(runs), "done": sum(1 for r in runs if r["status"] == "done"),
                   "cache_hit": round(sum(r.get("cache_read_tokens", 0) for r in usage) / total_in, 3) if total_in else 0.0,
                   "errors": sum(1 for r in usage if (r.get("status") or 200) >= 400)},
    }


# ---- static dashboard ---------------------------------------------------------------------------------

CSS = """
:root{--bg:#f7f7f5;--surface:#fff;--fg:#1d2329;--muted:#5f6b76;--rule:#dde1e5;--bar:#3a6ea5;--bar2:#9a6bc9;--ok:#2f7d4f;--warn:#b26b00;--bad:#b42318;
--sans:"IBM Plex Sans",system-ui,sans-serif;--mono:"IBM Plex Mono",ui-monospace,monospace}
@media (prefers-color-scheme:dark){:root:not([data-theme=light]){--bg:#13171b;--surface:#1b2127;--fg:#e4e8ec;--muted:#98a4b0;--rule:#2b333b;--bar:#78a6d6;--bar2:#c19be8;--ok:#5fc98a;--warn:#e6a23c;--bad:#f2766c;color-scheme:dark}}
:root[data-theme=dark]{--bg:#13171b;--surface:#1b2127;--fg:#e4e8ec;--muted:#98a4b0;--rule:#2b333b;--bar:#78a6d6;--bar2:#c19be8;--ok:#5fc98a;--warn:#e6a23c;--bad:#f2766c;color-scheme:dark}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:14px/1.5 var(--sans)}
main{max-width:1100px;margin:0 auto;padding:32px 20px;display:grid;gap:28px}h1{margin:0;font-size:26px}h2{margin:0 0 8px;font-size:17px}
.muted{color:var(--muted)}.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:12px}
.tile{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:12px 14px}.tile b{display:block;font:600 22px var(--mono)}
.tile span{color:var(--muted);font-size:12px;text-transform:uppercase;letter-spacing:.05em}
.box{background:var(--surface);border:1px solid var(--rule);border-radius:10px;padding:14px;overflow-x:auto}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 10px;border-bottom:1px solid var(--rule);white-space:nowrap}
th{color:var(--muted);font-weight:500;font-size:12px}td.n{font-family:var(--mono);text-align:right}
.st{font-family:var(--mono);font-size:12px}.done{color:var(--ok)}.escalated,.aborted{color:var(--bad)}.waiting_human{color:var(--warn)}
svg text{fill:var(--fg);font:12px var(--mono)}svg .lbl{fill:var(--muted)}
"""


def _bars(rows: list[tuple[str, float, str]], width: int = 700) -> str:
    if not rows:
        return "<p class='muted'>No data yet.</p>"
    top = max(v for _, v, _ in rows) or 1
    h = 26 * len(rows) + 10
    parts = [f"<svg viewBox='0 0 {width} {h}' width='100%' role='img' aria-label='cost by stage'>"]
    for i, (label, value, note) in enumerate(rows):
        y = 8 + i * 26
        w = max(2, (width - 330) * value / top)
        parts.append(f"<text class='lbl' x='0' y='{y + 13}'>{html.escape(label)}</text>"
                     f"<rect x='150' y='{y}' width='{w:.1f}' height='18' rx='3' fill='var(--bar)'/>"
                     f"<text x='{158 + w:.1f}' y='{y + 13}'>${value:.2f} {html.escape(note)}</text>")
    parts.append("</svg>")
    return "".join(parts)


def dashboard_html(data: dict) -> str:
    t = data["totals"]
    done = [r for r in data["runs"] if r["status"] == "done"]
    per_story = [r["cost"] for r in done if r["pipeline"] == "implement"]
    tiles = [("total spend", f"${t['cost']:.2f}"), ("runs", f"{t['runs']} ({t['done']} done)"),
             ("llm calls", str(t["calls"])), ("cache hit", f"{t['cache_hit'] * 100:.0f}%"),
             ("avg $ / story", f"${sum(per_story) / len(per_story):.2f}" if per_story else "–"),
             ("gateway errors", str(t["errors"]))]
    stage_rows = sorted(((k, v["cost"], f"· {v['calls']} calls · cache {v['cache_hit'] * 100:.0f}%")
                         for k, v in data["stages"].items()), key=lambda x: -x[1])
    model_rows = sorted(((k.replace("anthropic/", ""), v["cost"], f"· {v['calls']} calls")
                         for k, v in data["models"].items()), key=lambda x: -x[1])
    run_rows = "".join(
        f"<tr><td class='st'>{html.escape(r['run_id'])}</td><td>{r['pipeline']}</td><td>{html.escape(str(r['subject']))}</td>"
        f"<td class='st {r['status']}'>{r['status']}</td><td class='n'>${r['cost']:.2f}</td><td class='n'>{r['calls']}</td>"
        f"<td class='n'>{r['stage_runs']}</td><td class='n'>{r['loops']}</td><td class='n'>{r['escalations']}</td>"
        f"<td class='n'>{r['elapsed_min']}</td><td class='n'>{r['human_wait_min']}</td></tr>" for r in data["runs"])
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>SDLC Pipeline Costs</title><style>{CSS}</style></head><body><main>
<header><h1>SDLC pipeline: costs and runs</h1><p class="muted">Generated {datetime.now().strftime('%Y-%m-%d %H:%M')} from runs/ and the gateway usage log. Fake runs excluded.</p></header>
<section class="tiles">{''.join(f"<div class='tile'><span>{k}</span><b>{v}</b></div>" for k, v in tiles)}</section>
<section class="box"><h2>Spend by stage</h2>{_bars(stage_rows)}</section>
<section class="box"><h2>Spend by model</h2>{_bars(model_rows)}</section>
<section class="box"><h2>Runs</h2><table><thead><tr><th>run</th><th>pipeline</th><th>subject</th><th>status</th><th>cost</th><th>calls</th>
<th>stage runs</th><th>loops</th><th>escalations</th><th>elapsed min</th><th>human wait min</th></tr></thead><tbody>{run_rows}</tbody></table></section>
</main></body></html>"""


def write_dashboard(path: Path, data: dict | None = None) -> Path:
    data = data or collect()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dashboard_html(data))
    (path.parent / "data.json").write_text(json.dumps(data, indent=2))
    return path
