"""Multi-repo feature runs (P10, PLAN.md §18): `sdlc implement --feature EPIC`.

A feature run is a small coordinator over ordinary Pipeline B runs, one per story under the Epic.
Stories start in dependency order (Jira "Blocks" links, which publish created from config/repos.yaml),
so a consumer's story starts only after its provider's stories are Done, i.e. merged. While any child run
is escalated, the feature pauses (no new children start) so half a feature never lands silently.
State: runs/_features/<EPIC>.json. `sdlc watch` advances active features on every poll.
"""

from __future__ import annotations

import json
from pathlib import Path

from sdlc import registry
from sdlc.config import HOME, RUNS, load_config
from sdlc.connectors import jira
from sdlc.engine import Engine, RunStatus
from sdlc.engine.state import now

FEATURES = RUNS / "_features"
DONE = {"done", "closed", "resolved"}


def _path(epic: str) -> Path:
    return FEATURES / f"{epic}.json"


def load(epic: str) -> dict:
    return json.loads(_path(epic).read_text())


def save(state: dict) -> None:
    FEATURES.mkdir(parents=True, exist_ok=True)
    state["updated"] = now()
    _path(state["epic"]).write_text(json.dumps(state, indent=2))


def blocked_by(issue: dict) -> list[str]:
    """Keys that block this issue. On a blocked issue, Jira shows the blocker as `inwardIssue`."""
    return [lk["inwardIssue"]["key"] for lk in issue["fields"].get("issuelinks", [])
            if lk["type"]["name"] == "Blocks" and "inwardIssue" in lk]


def discover(epic: str, default_repo: str | None = None) -> dict:
    """default_repo: for a single-repo epic, whose stories carry no repo-<name> label."""
    res = jira.request("GET", f"/rest/api/3/search/jql?jql=parent%3D{epic}&maxResults=100"
                              "&fields=summary,labels,status,issuelinks")
    repos = registry.load()
    stories = []
    for issue in res.get("issues", []):
        f = issue["fields"]
        name = next((lbl[len("repo-"):] for lbl in f.get("labels", []) if lbl.startswith("repo-")), None)
        stories.append({"key": issue["key"], "summary": f["summary"], "repo": name,
                        "github": repos.get(name, {}).get("github") if name else default_repo,
                        "blocked_by": blocked_by(issue), "jira_status": f["status"]["name"], "run_id": None})
    if not stories:
        raise ValueError(f"{epic} has no child stories")
    return {"epic": epic, "created": now(), "status": "active", "note": None, "stories": stories, "inputs": {}}


def _refresh(state: dict) -> None:
    for st in state["stories"]:
        st["jira_status"] = jira.get_issue(st["key"], "status")["fields"]["status"]["name"]


def advance(engine: Engine, epic: str, start_children: bool = True) -> list[str]:
    state = load(epic)
    if state["status"] == "done":
        return []
    _refresh(state)
    actions: list[str] = []
    runs = {st["run_id"]: engine.store.load(st["run_id"]) for st in state["stories"] if st["run_id"]}
    stuck = [st for st in state["stories"] if st["run_id"] and runs[st["run_id"]].status == RunStatus.ESCALATED]
    if all(st["jira_status"].lower() in DONE for st in state["stories"]):
        state["status"], state["note"] = "done", "every story is Done"
        save(state)
        return [f"feature {epic}: done"]
    if stuck:
        state["status"] = "attention"
        state["note"] = f"paused: {stuck[0]['key']} escalated ({runs[stuck[0]['run_id']].escalation.get('reason', '')[:120]})"
        save(state)
        return [f"feature {epic}: {state['note']}"]
    state["status"], state["note"] = "active", None
    by_key = {st["key"]: st for st in state["stories"]}
    for st in state["stories"]:
        if st["run_id"] or st["jira_status"].lower() in DONE:
            continue
        waiting_on = [b for b in st["blocked_by"] if b in by_key and by_key[b]["jira_status"].lower() not in DONE]
        if waiting_on:
            continue
        if not st["github"]:
            st["run_id"] = None
            actions.append(f"feature {epic}: {st['key']} has no repo-<name> label; skipped")
            continue
        if not start_children:
            actions.append(f"feature {epic}: {st['key']} is ready to start")
            continue
        from sdlc.init_repo import project_snapshot
        from sdlc.stages import REAL_STAGES
        from sdlc.engine.runner import stages_of
        cfg = load_config(project_snapshot(st["github"]))
        fakes = [s for s in stages_of("implement") if s not in REAL_STAGES]
        save(state)   # record progress before a long child run
        child = engine.start("implement", cfg, {"ticket": st["key"], "repo": st["github"], "feature": epic,
                                                "project": str(HOME), **state.get("inputs", {})},
                             feature_id=epic, fake_stages=fakes)
        st["run_id"] = child.run_id
        actions.append(f"feature {epic}: started {st['key']} ({st['repo']}) → {child.run_id} [{child.status.value}]")
    save(state)
    return actions


def advance_all(engine: Engine) -> list[str]:
    actions: list[str] = []
    if FEATURES.exists():
        for path in sorted(FEATURES.glob("*.json")):
            state = json.loads(path.read_text())
            if state.get("status") in {"active", "attention"}:
                try:
                    actions += advance(engine, state["epic"])
                except Exception as exc:   # one feature's trouble shouldn't stop the watcher
                    actions.append(f"feature {state['epic']}: advance failed ({exc})")
    return actions


def start(engine: Engine, epic: str, default_repo: str | None = None, inputs: dict | None = None) -> list[str]:
    """inputs: extra inputs for every child run (e.g. the issue that asked for it, in CI mode)."""
    if _path(epic).exists():
        return advance(engine, epic)
    state = discover(epic, default_repo)
    state["inputs"] = inputs or {}
    save(state)
    return advance(engine, epic)
