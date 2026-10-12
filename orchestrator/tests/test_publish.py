import os
from pathlib import Path

from sdlc.connectors.jira import to_adf
from sdlc.openspec_parse import scenarios, story_markdown, tasks
from sdlc.stages.publish import safe_copy_tree
from sdlc.watch import feedback_text

SPEC = """## ADDED Requirements
### Requirement: Per-key limit
The API SHALL reject excess requests.

#### Scenario: Limit exceeded
- **WHEN** a key sends request 101 within 60s
- **THEN** respond 429 with Retry-After
"""


def change(tmp_path: Path) -> Path:
    c = tmp_path / "openspec" / "changes" / "add-limit"
    (c / "specs" / "rate").mkdir(parents=True)
    (c / "specs" / "rate" / "spec.md").write_text(SPEC)
    (c / "tasks.md").write_text("## 1. Impl\n- [ ] 1.1 Add middleware\n- [x] 1.2 Add config\n")
    (c / "proposal.md").write_text("## Why\nClients overload the API.\n\n## What Changes\n- Add a limit\n")
    return c


def test_scenarios_and_tasks(tmp_path):
    c = change(tmp_path)
    s = scenarios(c)
    assert s == [{"requirement": "Per-key limit", "name": "Limit exceeded",
                  "steps": ["WHEN a key sends request 101 within 60s", "THEN respond 429 with Retry-After"]}]
    assert tasks(c) == [(False, "1.1 Add middleware"), (True, "1.2 Add config")]


def test_story_markdown_and_adf(tmp_path):
    md = story_markdown("add-limit", change(tmp_path), "https://github.com/o/r/tree/main/openspec/changes/add-limit")
    assert "## Acceptance criteria" in md and "**Limit exceeded**" in md and "- [x] 1.2 Add config" in md
    adf = to_adf(md)
    kinds = [n["type"] for n in adf["content"]]
    assert {"heading", "paragraph", "bulletList", "taskList"} <= set(kinds)
    task_list = next(n for n in adf["content"] if n["type"] == "taskList")
    assert [t["attrs"]["state"] for t in task_list["content"]] == ["TODO", "DONE"]


def test_safe_copy_skips_symlinks(tmp_path):
    src, dest = tmp_path / "src", tmp_path / "dest"
    src.mkdir()
    (src / "ok.md").write_text("fine")
    secret = tmp_path / "secret.env"
    secret.write_text("TOKEN=x")
    os.symlink(secret, src / "leak.md")
    copied = safe_copy_tree(src, dest)
    assert (dest / "ok.md").exists() and not (dest / "leak.md").exists() and len(copied) == 1


def test_watch_feedback_text():
    cmd = {"body": "/sdlc revise also rename the change"}
    notes = [{"author": "rev", "body": "Use Redis wrapper", "path": "openspec/changes/x/design.md", "line": 12},
             {"author": "rev", "body": "General: too many tasks"}]
    text = feedback_text(notes, cmd)
    assert text.splitlines() == ["- also rename the change",
                                 "- openspec/changes/x/design.md:12 (rev) Use Redis wrapper",
                                 "- (rev) General: too many tasks"]


def test_watch_feedback_text_keeps_line_context_of_command():
    cmd = {"body": "/sdlc revise also have a plan for testing", "path": "openspec/changes/x/tasks.md", "line": 4}
    assert feedback_text([], cmd) == "- openspec/changes/x/tasks.md:4 also have a plan for testing"
