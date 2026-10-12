"""GitHub connector (SCM provider for local mode). REST via urllib + git with token auth.

The token reaches git only through GIT_CONFIG_* environment variables (an http extraheader),
so it is never written to .git/config and never appears in the process list. Phase 1 runs git
only in clones it owns, with hooks disabled, never in the agent's workspace.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from sdlc import secrets

API = "https://api.github.com"
BOT_MARKER = "<!-- sdlc-bot -->"
TRUSTED_ASSOCIATIONS = {"OWNER", "MEMBER", "COLLABORATOR"}


class GitHubError(Exception):
    pass


def _token() -> str:
    return secrets.get("GITHUB_TOKEN")


def git_env() -> dict[str, str]:
    basic = base64.b64encode(f"x-access-token:{_token()}".encode()).decode()
    return {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_COUNT": "3",
            "GIT_CONFIG_KEY_0": "http.https://github.com/.extraheader",
            "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {basic}",
            "GIT_CONFIG_KEY_1": "core.hooksPath", "GIT_CONFIG_VALUE_1": "/dev/null",
            "GIT_CONFIG_KEY_2": "user.name", "GIT_CONFIG_VALUE_2": "sdlc-pipeline"}


def git(cwd: Path | None, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    cmd = ["git", "-c", "user.email=sdlc-pipeline@users.noreply.github.com", *args]
    proc = subprocess.run(cmd, cwd=cwd, env=git_env(), capture_output=True, text=True)
    if check and proc.returncode != 0:
        raise GitHubError(f"git {args[0]} failed: {proc.stderr.strip()[-400:]}")
    return proc


def clone(repo: str, dest: Path, branch: str | None = None, depth: int | None = 50) -> Path:
    args = ["clone", "--quiet", f"https://github.com/{repo}.git", str(dest)]
    if depth:
        args[1:1] = ["--depth", str(depth), "--no-single-branch"]
    if branch:
        args[1:1] = ["--branch", branch]
    git(None, *args)
    return dest


def request(method: str, path: str, body: dict | None = None, ok: tuple[int, ...] = (200, 201)) -> Any:
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": f"Bearer {_token()}",
                                          "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28",
                                          "Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read()
            return json.loads(raw) if raw else None
    except urllib.error.HTTPError as exc:
        if exc.code in ok:
            return None
        raise GitHubError(f"GitHub {method} {path}: {exc.code} {exc.read()[:300].decode(errors='replace')}") from exc


def repo_info(repo: str) -> dict:
    return request("GET", f"/repos/{repo}")


def find_open_pr(repo: str, branch: str) -> dict | None:
    owner = repo.split("/")[0]
    prs = request("GET", f"/repos/{repo}/pulls?state=open&head={owner}:{branch}")
    return prs[0] if prs else None


def open_or_update_pr(repo: str, branch: str, base: str, title: str, body: str) -> dict:
    existing = find_open_pr(repo, branch)
    if existing:
        return request("PATCH", f"/repos/{repo}/pulls/{existing['number']}", {"title": title, "body": body})
    return request("POST", f"/repos/{repo}/pulls", {"title": title, "head": branch, "base": base, "body": body})


def get_pr(repo: str, number: int) -> dict:
    return request("GET", f"/repos/{repo}/pulls/{number}")


def add_labels(repo: str, number: int, labels: list[str]) -> None:
    request("POST", f"/repos/{repo}/issues/{number}/labels", {"labels": labels})


def comment(repo: str, number: int, text: str) -> dict:
    return request("POST", f"/repos/{repo}/issues/{number}/comments", {"body": f"{text}\n\n{BOT_MARKER}"})


def conversation(repo: str, number: int) -> list[dict]:
    """Issue comments + line review comments + review bodies, oldest first, bot's own excluded."""
    items = []
    for c in request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100") or []:
        items.append({"id": f"ic-{c['id']}", "kind": "comment", "author": c["user"]["login"],
                      "association": c.get("author_association"), "at": c["created_at"], "body": c["body"]})
    for c in request("GET", f"/repos/{repo}/pulls/{number}/comments?per_page=100") or []:
        items.append({"id": f"rc-{c['id']}", "kind": "line", "author": c["user"]["login"],
                      "association": c.get("author_association"), "at": c["created_at"], "body": c["body"],
                      "path": c.get("path"), "line": c.get("line") or c.get("original_line")})
    for r in request("GET", f"/repos/{repo}/pulls/{number}/reviews?per_page=100") or []:
        if r.get("body"):
            items.append({"id": f"rv-{r['id']}", "kind": "review", "author": r["user"]["login"],
                          "association": r.get("author_association"), "at": r.get("submitted_at") or "",
                          "body": r["body"], "state": r.get("state")})
    items = [i for i in items if BOT_MARKER not in (i["body"] or "")]
    return sorted(items, key=lambda i: i["at"])


# ---- issues, labels, repo setup (sdlc init / CI mode) ---------------------------------------------------

def requirement_issues(repo: str, label: str = "sdlc") -> list[dict]:
    """Open issues with the requirement label (pull requests excluded)."""
    issues = request("GET", f"/repos/{repo}/issues?state=open&labels={label}&per_page=100") or []
    return [i for i in issues if "pull_request" not in i]


def issue_comments(repo: str, number: int) -> list[dict]:
    """Issue comments oldest first, bot's own excluded, in the same shape as conversation()."""
    out = []
    for c in request("GET", f"/repos/{repo}/issues/{number}/comments?per_page=100") or []:
        if BOT_MARKER not in (c["body"] or ""):
            out.append({"id": f"ic-{c['id']}", "kind": "comment", "author": c["user"]["login"],
                        "association": c.get("author_association"), "at": c["created_at"], "body": c["body"] or ""})
    return out


def ensure_label(repo: str, name: str, color: str, description: str) -> bool:
    """Create the label if it's missing. True if created."""
    try:
        request("POST", f"/repos/{repo}/labels", {"name": name, "color": color, "description": description})
        return True
    except GitHubError as exc:
        if " 422 " in str(exc):   # already exists
            return False
        raise


def secret_names(repo: str) -> set[str]:
    res = request("GET", f"/repos/{repo}/actions/secrets?per_page=100") or {}
    return {s["name"] for s in res.get("secrets", [])}


def set_secret(repo: str, name: str, value: str) -> None:
    """Encrypt with the repo's public key (libsodium sealed box), as the API requires; the value never leaves
    this process unencrypted."""
    from nacl import encoding, public
    key = request("GET", f"/repos/{repo}/actions/secrets/public-key")
    box = public.SealedBox(public.PublicKey(key["key"].encode(), encoding.Base64Encoder()))
    sealed = base64.b64encode(box.encrypt(value.encode())).decode()
    request("PUT", f"/repos/{repo}/actions/secrets/{name}", {"encrypted_value": sealed, "key_id": key["key_id"]},
            ok=(201, 204))


def set_variable(repo: str, name: str, value: str) -> None:
    try:
        request("PATCH", f"/repos/{repo}/actions/variables/{name}", {"name": name, "value": value}, ok=(204,))
    except GitHubError as exc:
        if " 404 " not in str(exc):
            raise
        request("POST", f"/repos/{repo}/actions/variables", {"name": name, "value": value}, ok=(201,))


def allow_actions_prs(repo: str) -> None:
    """Let workflows' GITHUB_TOKEN open PRs (repo setting 'Allow GitHub Actions to create and approve pull requests')."""
    request("PUT", f"/repos/{repo}/actions/permissions/workflow",
            {"default_workflow_permissions": "read", "can_approve_pull_request_reviews": True}, ok=(204,))


def latest_artifact(repo: str, name: str, token: str | None = None) -> dict | None:
    res = _request_with(token, "GET", f"/repos/{repo}/actions/artifacts?name={name}&per_page=20") or {}
    live = [a for a in res.get("artifacts", []) if not a.get("expired")]
    return max(live, key=lambda a: a["created_at"]) if live else None


def download_artifact(repo: str, artifact_id: int, token: str | None = None) -> bytes:
    """The API answers with a redirect to signed blob storage; follow it without our token."""
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    req = urllib.request.Request(f"{API}/repos/{repo}/actions/artifacts/{artifact_id}/zip",
                                 headers={"Authorization": f"Bearer {token or _token()}",
                                          "Accept": "application/vnd.github+json"})
    try:
        with urllib.request.build_opener(NoRedirect).open(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        if exc.code not in (301, 302, 303, 307):
            raise GitHubError(f"artifact download: {exc.code}") from exc
        location = exc.headers["Location"]
    with urllib.request.urlopen(location, timeout=300) as resp:
        return resp.read()


def _request_with(token: str | None, method: str, path: str) -> Any:
    if not token:
        return request(method, path)
    req = urllib.request.Request(f"{API}{path}", method=method,
                                 headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
                                          "X-GitHub-Api-Version": "2022-11-28"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read() or b"null")


def repo_from_remote(path: Path) -> str | None:
    """owner/name from a local clone's origin URL (https or ssh), if it's on GitHub."""
    proc = subprocess.run(["git", "-C", str(path), "remote", "get-url", "origin"], capture_output=True, text=True)
    url = proc.stdout.strip()
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix):].removesuffix(".git").strip("/") or None
    return None
