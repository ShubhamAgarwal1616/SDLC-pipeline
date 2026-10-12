"""The Confluence mirror: Markdown → storage format, the fake backend, publish + status updates. No live service."""

from sdlc.config import load_config
from sdlc.connectors import confluence
from sdlc.engine import RunState, RunStore
from sdlc.engine.stage import StageContext
from sdlc.stages import publish


def test_markdown_to_storage():
    md = ("# Title\n\nSome **bold** and `code` with a [link](https://x.y/z).\nSame paragraph.\n\n"
          "- one\n- [x] done\n- [ ] todo\n\n1. first\n2. second\n\n| A | B |\n|---|---|\n| 1 | <2> |\n\n"
          "```kotlin\nval x = 1 // ]]> tricky\n```\n")
    out = confluence.to_storage(md)
    assert "<h1>Title</h1>" in out
    assert "<p>Some <strong>bold</strong> and <code>code</code> with a <a href=\"https://x.y/z\">link</a>. Same paragraph.</p>" in out
    assert "<ul><li>one</li><li>☑ done</li><li>☐ todo</li></ul>" in out
    assert "<ol><li>first</li><li>second</li></ol>" in out
    assert "<tr><th>A</th><th>B</th></tr><tr><td>1</td><td>&lt;2&gt;</td></tr>" in out
    assert 'ac:name="language">kotlin<' in out and "]]]]><![CDATA[>" in out


def test_status_line_is_replaced_not_stacked():
    page = confluence.with_status("<p>body</p>", "To do")
    page = confluence.with_status(page, "Done · merged")
    assert page.count("<strong>Status:</strong>") == 1 and "Done · merged" in page and page.endswith("<p>body</p>")


def test_client_is_none_unless_docs_is_confluence(tmp_path):
    assert confluence.client(load_config(env={})) is None
    cfg = load_config(overrides={"project.docs": "confluence", "confluence.fake": True, "confluence.space": "SP"}, env={})
    assert isinstance(confluence.client(cfg), confluence.FakeConfluence)


def test_mirror_to_fake_confluence_then_status_updates(tmp_path, monkeypatch):
    monkeypatch.setattr(confluence, "RUNS", tmp_path / "runs")
    change = tmp_path / "repo" / "openspec" / "changes" / "rate-limit-core"
    (change / "specs" / "rate-limiting").mkdir(parents=True)
    (change / "proposal.md").write_text("## Why\nAbuse.\n\n## What Changes\nLimit per key.\n")
    (change / "design.md").write_text("## Decisions\nToken bucket.\n")
    (change / "tasks.md").write_text("- [ ] add limiter\n")
    (change / "specs" / "rate-limiting" / "spec.md").write_text(
        "### Requirement: Limit\n#### Scenario: Over the limit\n- **WHEN** 101 requests\n- **THEN** 429\n")
    cfg = load_config(overrides={"project.docs": "confluence", "confluence.fake": True, "confluence.space": "SP"}, env={})
    ctx = StageContext(run=RunState(run_id="run-c", pipeline="plan"), config=cfg, store=RunStore(tmp_path / "r"),
                       stage="a4_publish", attempt=1, feedback=[])
    plan = {"stories": [{"id": "rate-limit-core", "repo": None, "summary": "Core rate limiting"}]}
    roots = {"": (tmp_path / "repo", "https://github.com/o/r/tree/main")}
    docs = publish.mirror_docs(ctx, "Rate limiting", "# PRD\n\n## Problem\nAbuse.\n", plan, roots,
                               {(None, "rate-limit-core"): "KAN-8"}, "KAN-7")
    assert "error" not in docs and len(docs["pages"]) == 2
    index = confluence.FakeConfluence("SP", root=tmp_path / "runs" / "_confluence")._index()
    child = index["openspec-rate-limit-core"]
    assert child["title"] == "Core rate limiting [KAN-8]" and child["parent"] == docs["pages"][0]["id"]
    assert "Over the limit" in child["storage"] and "Token bucket" in child["storage"] and "To do · Jira KAN-8" in child["storage"]
    parent_html = index["sdlc-run-c"]["storage"]
    assert "<td>KAN-8</td>" in parent_html and "Rate limiting [KAN-7]" == index["sdlc-run-c"]["title"]

    impl_ctx = StageContext(run=RunState(run_id="run-i", pipeline="implement"), config=cfg,
                            store=RunStore(tmp_path / "r"), stage="b10_close", attempt=1, feedback=[])
    assert publish.docs_status(impl_ctx, "rate-limit-core", "Done · merged in PR 3") is None
    child = confluence.FakeConfluence("SP", root=tmp_path / "runs" / "_confluence")._index()["openspec-rate-limit-core"]
    assert child["storage"].count("Status:") == 1 and "Done · merged in PR 3" in child["storage"]
    assert child["version"] == 2


def test_mirror_failure_does_not_raise(tmp_path, monkeypatch):
    cfg = load_config(overrides={"project.docs": "confluence", "confluence.space": "SP"}, env={})
    ctx = StageContext(run=RunState(run_id="run-c", pipeline="plan"), config=cfg, store=RunStore(tmp_path),
                       stage="a4_publish", attempt=1, feedback=[])

    class Boom:
        def upsert(self, *a, **k):
            raise confluence.ConfluenceError("403 no space permission")
    monkeypatch.setattr(confluence, "client", lambda cfg: Boom())
    docs = publish.mirror_docs(ctx, "T", "", {"stories": []}, {}, {}, None)
    assert "403" in docs["error"]
