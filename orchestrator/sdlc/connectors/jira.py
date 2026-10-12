"""Jira Cloud connector (REST v3) plus a small Markdown → Atlassian Document Format converter."""

from __future__ import annotations

import base64
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

from sdlc import secrets


class JiraError(Exception):
    pass


def _base() -> str:
    return secrets.get("JIRA_BASE_URL").rstrip("/")


def request(method: str, path: str, body: dict | None = None) -> Any:
    auth = base64.b64encode(f"{secrets.get('JIRA_USER_EMAIL')}:{secrets.get('JIRA_API_TOKEN')}".encode()).decode()
    req = urllib.request.Request(f"{_base()}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Basic {auth}", "Accept": "application/json",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        raise JiraError(f"Jira {method} {path}: {exc.code} {exc.read()[:400].decode(errors='replace')}") from exc


def get_issue(key: str, fields: str = "summary,labels,status,parent,issuetype,description") -> dict:
    return request("GET", f"/rest/api/3/issue/{key}?fields={fields}")


def transition(key: str, to_status: str) -> bool:
    """Move an issue to `to_status` if a transition to it exists. Returns False if none matches."""
    options = request("GET", f"/rest/api/3/issue/{key}/transitions")["transitions"]
    match = next((t for t in options if t["to"]["name"].lower() == to_status.lower()
                  or t["name"].lower() == to_status.lower()), None)
    if not match:
        return False
    request("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": match["id"]}})
    return True


def browse_url(key: str) -> str:
    return f"{_base()}/browse/{key}"


def find_by_label(project: str, label: str) -> list[dict]:
    jql = urllib.parse.quote(f'project = "{project}" AND labels = "{label}" ORDER BY created ASC')
    res = request("GET", f"/rest/api/3/search/jql?jql={jql}&maxResults=10&fields=summary,issuetype,status,labels")
    return res.get("issues", [])


def create_issue(project: str, issue_type: str, summary: str, description_md: str, labels: list[str],
                 parent: str | None = None) -> str:
    fields: dict[str, Any] = {"project": {"key": project}, "issuetype": {"name": issue_type},
                              "summary": summary[:250], "description": to_adf(description_md), "labels": labels}
    if parent:
        fields["parent"] = {"key": parent}
    return request("POST", "/rest/api/3/issue", {"fields": fields})["key"]


def link_blocks(blocker: str, blocked: str) -> None:
    """`blocker` blocks `blocked`. In the create-link API the *inwardIssue* is the blocker
    (verified against Jira Cloud: inward=A, outward=B shows "A blocks B")."""
    request("POST", "/rest/api/3/issueLink",
            {"type": {"name": "Blocks"}, "inwardIssue": {"key": blocker}, "outwardIssue": {"key": blocked}})


def delete_link(link_id: str) -> None:
    request("DELETE", f"/rest/api/3/issueLink/{link_id}")


def links(key: str) -> list[dict]:
    return request("GET", f"/rest/api/3/issue/{key}?fields=issuelinks")["fields"].get("issuelinks", [])


# ---- Markdown → ADF (headings, paragraphs, bullet/numbered lists, task lists, code, bold, code spans, links)

def _inline(text: str) -> list[dict]:
    nodes: list[dict] = []
    pattern = re.compile(r"(\*\*[^*]+\*\*|`[^`]+`|\[[^\]]+\]\([^)]+\))")
    for part in pattern.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            nodes.append({"type": "text", "text": part[2:-2], "marks": [{"type": "strong"}]})
        elif part.startswith("`") and part.endswith("`"):
            nodes.append({"type": "text", "text": part[1:-1], "marks": [{"type": "code"}]})
        elif m := re.fullmatch(r"\[([^\]]+)\]\(([^)]+)\)", part):
            nodes.append({"type": "text", "text": m.group(1), "marks": [{"type": "link", "attrs": {"href": m.group(2)}}]})
        else:
            nodes.append({"type": "text", "text": part})
    return nodes


def _para(text: str) -> dict:
    return {"type": "paragraph", "content": _inline(text)}


def to_adf(markdown: str) -> dict:
    content: list[dict] = []
    lines = markdown.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        if line.startswith("```"):
            block = []
            i += 1
            while i < len(lines) and not lines[i].startswith("```"):
                block.append(lines[i])
                i += 1
            content.append({"type": "codeBlock", "content": [{"type": "text", "text": "\n".join(block) or " "}]})
        elif m := re.match(r"^(#{1,6})\s+(.*)", line):
            content.append({"type": "heading", "attrs": {"level": min(len(m.group(1)), 6)}, "content": _inline(m.group(2))})
        elif re.match(r"^\s*[-*] \[[ xX]\] ", line):
            items = []
            while i < len(lines) and (m := re.match(r"^\s*[-*] \[([ xX])\] (.*)", lines[i])):
                items.append({"type": "taskItem", "attrs": {"localId": f"t{len(content)}-{len(items)}",
                                                            "state": "DONE" if m.group(1).lower() == "x" else "TODO"},
                              "content": _inline(m.group(2))})
                i += 1
            content.append({"type": "taskList", "attrs": {"localId": f"tl{len(content)}"}, "content": items})
            continue
        elif re.match(r"^\s*([-*]|\d+\.) ", line):
            ordered = bool(re.match(r"^\s*\d+\. ", line))
            items = []
            while i < len(lines) and re.match(r"^\s*([-*]|\d+\.) ", lines[i]):
                items.append({"type": "listItem", "content": [_para(re.sub(r"^\s*([-*]|\d+\.) ", "", lines[i]))]})
                i += 1
            content.append({"type": "orderedList" if ordered else "bulletList", "content": items})
            continue
        elif line.strip():
            para = [line.strip()]
            while i + 1 < len(lines) and lines[i + 1].strip() and not re.match(r"^(#|```|\s*([-*]|\d+\.) )", lines[i + 1]):
                i += 1
                para.append(lines[i].strip())
            content.append(_para(" ".join(para)))
        i += 1
    return {"type": "doc", "version": 1, "content": content or [_para(" ")]}
