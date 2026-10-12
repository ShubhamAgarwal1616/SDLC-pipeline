import json

import pytest

from sdlc import feature, registry, watch
from sdlc.config import ConfigError

REG = {"api": {"github": "o/api"}, "client": {"github": "o/client", "depends_on": ["api"]},
       "ui": {"github": "o/ui", "depends_on": ["client"]}}


def test_registry_order_and_lookup(tmp_path):
    assert registry.ordered(["ui", "client", "api"], REG) == ["api", "client", "ui"]
    assert registry.name_for("o/client", REG) == "client"
    bad = tmp_path / "repos.yaml"
    bad.write_text("repos:\n  a: {github: o/a, depends_on: [nope]}\n")
    with pytest.raises(ConfigError, match="unknown repo"):
        registry.load(bad)
    cyc = {"a": {"depends_on": ["b"]}, "b": {"depends_on": ["a"]}}
    with pytest.raises(ConfigError, match="cycle"):
        registry.ordered(["a", "b"], cyc)


def test_plan_targets_single_vs_feature(tmp_path, monkeypatch):
    monkeypatch.setattr(registry, "load", lambda path=None: REG)
    single = registry.plan_targets(tmp_path, {"repo": "o/api"})
    assert len(single) == 1 and single[0].name is None and single[0].ws == tmp_path / "workspace"
    multi = registry.plan_targets(tmp_path, {"repos": ["client", "api"]})
    assert [t.name for t in multi] == ["api", "client"] and multi[1].prefix == "client/"
    assert multi[1].ws == tmp_path / "workspaces" / "client"


def test_gate_prs_single_and_multi():
    assert watch._gate_prs({"repo": "o/a", "pr_number": 3}) == [{"repo": "o/a", "number": 3, "name": "o/a"}]
    multi = watch._gate_prs({"prs": [{"repo": "o/a", "pr_number": 1, "name": "a"}, {"repo": "o/b", "pr_number": 2}]})
    assert [(p["name"], p["number"]) for p in multi] == [("a", 1), ("o/b", 2)]


def test_blocked_by_reads_inward_blockers():
    issue = {"fields": {"issuelinks": [
        {"type": {"name": "Blocks"}, "inwardIssue": {"key": "K-1"}},
        {"type": {"name": "Blocks"}, "outwardIssue": {"key": "K-9"}},
        {"type": {"name": "Relates"}, "inwardIssue": {"key": "K-5"}}]}}
    assert feature.blocked_by(issue) == ["K-1"]


def test_feature_starts_stories_in_dependency_order(tmp_path, monkeypatch):
    monkeypatch.setattr(feature, "FEATURES", tmp_path / "features")
    statuses = {"K-1": "To Do", "K-2": "To Do"}
    monkeypatch.setattr(feature.jira, "get_issue", lambda key, fields="": {"fields": {"status": {"name": statuses[key]}}})
    started = []

    class FakeEngine:
        class store:
            @staticmethod
            def load(run_id):
                return type("R", (), {"status": feature.RunStatus.WAITING_HUMAN, "escalation": None})()

        def start(self, pipeline, cfg, inputs, feature_id=None, fake_stages=None):
            started.append(inputs["ticket"])
            return type("S", (), {"run_id": f"run-{inputs['ticket']}", "status": feature.RunStatus.WAITING_HUMAN})()

    monkeypatch.setattr(feature, "load_config", lambda *a, **k: None)
    monkeypatch.setattr("sdlc.init_repo.project_snapshot", lambda gh: tmp_path)
    feature.save({"epic": "E-1", "status": "active", "note": None, "stories": [
        {"key": "K-1", "summary": "api", "repo": "api", "github": "o/api", "blocked_by": [], "jira_status": "To Do", "run_id": None},
        {"key": "K-2", "summary": "client", "repo": "client", "github": "o/client", "blocked_by": ["K-1"],
         "jira_status": "To Do", "run_id": None}]})
    feature.advance(FakeEngine(), "E-1")
    assert started == ["K-1"]                       # consumer waits for its provider
    feature.advance(FakeEngine(), "E-1")
    assert started == ["K-1"]                       # still blocked
    statuses["K-1"] = "Done"
    feature.advance(FakeEngine(), "E-1")
    assert started == ["K-1", "K-2"]
    statuses["K-2"] = "Done"
    assert feature.advance(FakeEngine(), "E-1") == ["feature E-1: done"]
    assert json.loads((tmp_path / "features" / "E-1.json").read_text())["status"] == "done"
