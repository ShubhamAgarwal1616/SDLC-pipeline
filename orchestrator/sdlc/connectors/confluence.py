"""Confluence mirror (Cloud REST v1) plus a fake backend, and a small Markdown → storage-format converter.

The spec lives in the repo; Confluence is a read-only mirror for people who don't read PRs: one page per requirement
(the PRD and its stories) with one child page per change (proposal, scenarios, design, tasks). Pages are found by
label, so publishing is idempotent. `confluence.fake: true` writes the same pages as HTML files under
runs/_confluence/ (no space needed). Auth: JIRA_USER_EMAIL + CONFLUENCE_API_TOKEN (or JIRA_API_TOKEN).
"""

from __future__ import annotations

import base64
import html
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from sdlc import secrets
from sdlc.config import RUNS


class ConfluenceError(Exception):
    pass


# ---- Markdown → Confluence storage format -----------------------------------------------------------------

def _inline(text: str) -> str:
    out = html.escape(text, quote=False)
    out = re.sub(r"`([^`]+)`", r"<code>\1</code>", out)
    out = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", out)
    out = re.sub(r"\[([^\]]+)\]\(([^)\s]+)\)", lambda m: f'<a href="{html.escape(m.group(2))}">{m.group(1)}</a>', out)
    return out


def to_storage(markdown: str) -> str:
    """Enough Markdown for PRDs and OpenSpec files: headings, paragraphs, lists, task lists, tables, code."""
    lines, out, i = markdown.splitlines(), [], 0
    para: list[str] = []

    def flush() -> None:
        if para:
            out.append(f"<p>{_inline(' '.join(p.strip() for p in para))}</p>")
            para.clear()

    while i < len(lines):
        line = lines[i]
        if line.strip().startswith("```"):
            flush()
            lang, body = line.strip()[3:].strip(), []
            i += 1
            while i < len(lines) and not lines[i].strip().startswith("```"):
                body.append(lines[i])
                i += 1
            code = "\n".join(body).replace("]]>", "]]]]><![CDATA[>")
            param = f'<ac:parameter ac:name="language">{html.escape(lang)}</ac:parameter>' if lang else ""
            out.append(f'<ac:structured-macro ac:name="code">{param}<ac:plain-text-body><![CDATA[{code}]]>'
                       "</ac:plain-text-body></ac:structured-macro>")
        elif m := re.match(r"^(#{1,6})\s+(.*)", line):
            flush()
            out.append(f"<h{len(m.group(1))}>{_inline(m.group(2).strip())}</h{len(m.group(1))}>")
        elif line.strip().startswith("|") and i + 1 < len(lines) and re.match(r"^\s*\|[\s:|-]+\|\s*$", lines[i + 1]):
            flush()
            cells = lambda row: [c.strip() for c in row.strip().strip("|").split("|")]   # noqa: E731
            rows = [f"<tr>{''.join(f'<th>{_inline(c)}</th>' for c in cells(line))}</tr>"]
            i += 2
            while i < len(lines) and lines[i].strip().startswith("|"):
                rows.append(f"<tr>{''.join(f'<td>{_inline(c)}</td>' for c in cells(lines[i]))}</tr>")
                i += 1
            out.append(f"<table><tbody>{''.join(rows)}</tbody></table>")
            continue
        elif re.match(r"^\s*([-*]|\d+\.)\s+", line):
            flush()
            ordered = bool(re.match(r"^\s*\d+\.", line))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.)\s+", lines[i]):
                item = re.sub(r"^\s*([-*]|\d+\.)\s+", "", lines[i])
                if t := re.match(r"^\[([ xX])\]\s+(.*)", item):
                    item = ("☑ " if t.group(1).lower() == "x" else "☐ ") + t.group(2)
                items.append(f"<li>{_inline(item)}</li>")
                i += 1
            tag = "ol" if ordered else "ul"
            out.append(f"<{tag}>{''.join(items)}</{tag}>")
            continue
        elif not line.strip():
            flush()
        else:
            para.append(line)
        i += 1
    flush()
    return "\n".join(out)


STATUS_RE = re.compile(r"^<p><strong>Status:</strong>.*?</p>\n?", re.S)


def with_status(storage: str, status_md: str) -> str:
    """Replace (or add) the page's leading status line."""
    return f"<p><strong>Status:</strong> {_inline(status_md)}</p>\n" + STATUS_RE.sub("", storage, count=1)


# ---- backends ---------------------------------------------------------------------------------------------------

class Confluence:
    def __init__(self, space: str, parent_id: str | None = None):
        self.space, self.parent_id = space, parent_id
        base = secrets.get("CONFLUENCE_BASE_URL", required=False) or (secrets.get("JIRA_BASE_URL").rstrip("/") + "/wiki")
        self.base = base.rstrip("/")
        token = secrets.get("CONFLUENCE_API_TOKEN", required=False) or secrets.get("JIRA_API_TOKEN")
        self.auth = base64.b64encode(f"{secrets.get('JIRA_USER_EMAIL')}:{token}".encode()).decode()

    def _req(self, method: str, path: str, body: Any = None) -> Any:
        req = urllib.request.Request(f"{self.base}{path}", method=method,
                                     data=json.dumps(body).encode() if body is not None else None,
                                     headers={"Authorization": f"Basic {self.auth}", "Accept": "application/json",
                                              "Content-Type": "application/json"})
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            raise ConfluenceError(f"Confluence {method} {path}: {exc.code} "
                                  f"{exc.read()[:300].decode(errors='replace')}") from exc

    def space_ok(self) -> bool:
        return bool(self._req("GET", f"/rest/api/space/{urllib.parse.quote(self.space)}"))

    def find(self, label: str) -> dict | None:
        cql = urllib.parse.quote(f'space="{self.space}" and type=page and label="{label}"')
        res = self._req("GET", f"/rest/api/content/search?cql={cql}&expand=version,body.storage&limit=1")
        found = (res or {}).get("results") or []
        return found[0] if found else None

    def url(self, page: dict) -> str:
        return self.base + page.get("_links", {}).get("webui", f"/pages/{page['id']}")

    def upsert(self, title: str, storage: str, label: str, parent_id: str | None = None) -> dict:
        page = self.find(label)
        body = {"type": "page", "title": title, "space": {"key": self.space},
                "body": {"storage": {"value": storage, "representation": "storage"}}}
        if parent_id or self.parent_id:
            body["ancestors"] = [{"id": parent_id or self.parent_id}]
        if page:
            body["version"] = {"number": page["version"]["number"] + 1}
            page = self._req("PUT", f"/rest/api/content/{page['id']}", body)
        else:
            page = self._req("POST", "/rest/api/content", body)
            self._req("POST", f"/rest/api/content/{page['id']}/label", [{"prefix": "global", "name": label}])
        return {"id": page["id"], "title": title, "url": self.url(page)}

    def storage_of(self, label: str) -> tuple[dict, str] | None:
        page = self.find(label)
        return (page, page["body"]["storage"]["value"]) if page else None


class FakeConfluence:
    """Same interface; pages are HTML files plus an index under runs/_confluence/<space>/."""

    def __init__(self, space: str, parent_id: str | None = None, root: Path | None = None):
        self.space, self.parent_id = space or "FAKE", parent_id
        self.dir = (root or RUNS / "_confluence") / self.space
        self.index_file = self.dir / "index.json"

    def _index(self) -> dict:
        return json.loads(self.index_file.read_text()) if self.index_file.exists() else {}

    def space_ok(self) -> bool:
        return True

    def find(self, label: str) -> dict | None:
        return self._index().get(label)

    def upsert(self, title: str, storage: str, label: str, parent_id: str | None = None) -> dict:
        index = self._index()
        page = index.get(label) or {"id": f"fake-{len(index) + 1}", "version": 0}
        slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:80]
        path = self.dir / f"{page['id']}-{slug}.html"
        self.dir.mkdir(parents=True, exist_ok=True)
        path.write_text(f"<!doctype html><meta charset=utf-8><title>{html.escape(title)}</title>\n{storage}\n")
        page = {**page, "title": title, "url": str(path), "parent": parent_id or self.parent_id,
                "version": page["version"] + 1, "storage": storage}
        index[label] = page
        self.index_file.write_text(json.dumps(index, indent=2))
        return {"id": page["id"], "title": title, "url": str(path)}

    def storage_of(self, label: str) -> tuple[dict, str] | None:
        page = self.find(label)
        return (page, page["storage"]) if page else None


def client(cfg) -> Confluence | FakeConfluence | None:
    """The configured backend, or None when docs aren't mirrored (project.docs != confluence)."""
    if cfg.get("project.docs") != "confluence":
        return None
    space, parent = cfg.get("confluence.space"), cfg.get("confluence.parent_page_id")
    if cfg.get("confluence.fake"):
        return FakeConfluence(space, parent)
    if not space:
        raise ConfluenceError("project.docs is confluence but confluence.space isn't set (in .sdlc/pipeline.yaml)")
    return Confluence(space, parent)
