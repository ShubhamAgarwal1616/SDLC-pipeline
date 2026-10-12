"""P2 spike: prove the Phase 2 assumptions before building on them (BUILD_AND_ROLLOUT.md P2).

  1. The sandbox can reach llm-gateway and nothing else.
  2. Claude Code headless works through the gateway → OpenRouter, and usage is recorded.
  3. Managed settings are applied (a denied command is refused).
  4. /grill-with-docs runs headless, asks a question, and the session resumes in a NEW container.

Usage:  docker compose up -d && uv run python scripts/p2_spike.py
Needs:  images sdlc-gateway:dev + sdlc-worker:dev, OPENROUTER_API_KEY in .env (read by the gateway only).
"""

from __future__ import annotations

import json
import secrets
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GATEWAY_DATA = ROOT / "runs" / "_gateway"
SPIKE = ROOT / "runs" / f"spike-{time.strftime('%Y%m%d-%H%M%S')}"
IMAGE = "sdlc-worker:dev"
NETWORK = "sdlc_internal"
BUDGET_USD = 3.0

from sdlc.config import load_config  # noqa: E402  (models come from .env: SDLC_TEST_MODEL, SDLC_MODEL_GRILL)
from sdlc.evals import test_model  # noqa: E402

SPIKE_MODEL = test_model()
GRILL_MODEL = load_config().get("llm.stages.grill.model")
results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail else ""), flush=True)
    return ok


def issue_key(run_id: str, stage: str) -> str:
    """What the orchestrator will do per run: a budgeted key the gateway accepts."""
    GATEWAY_DATA.mkdir(parents=True, exist_ok=True)
    keys_file = GATEWAY_DATA / "keys.json"
    keys = json.loads(keys_file.read_text()) if keys_file.exists() else {}
    key = "sdlc-" + secrets.token_urlsafe(24)
    keys[key] = {"run_id": run_id, "stage": stage, "budget_usd": BUDGET_USD}
    keys_file.write_text(json.dumps(keys, indent=2))
    return key


def sandbox(args: list[str], key: str, mounts: dict[Path, str], entrypoint: str | None = None,
            timeout: int = 900) -> subprocess.CompletedProcess:
    cmd = ["docker", "run", "--rm", "--network", NETWORK,
           "--cap-drop", "ALL", "--security-opt", "no-new-privileges", "--pids-limit", "512",
           "--memory", "2g", "--cpus", "2",
           "-e", "ANTHROPIC_BASE_URL=http://llm-gateway:4000",
           "-e", f"ANTHROPIC_AUTH_TOKEN={key}", "-e", "ANTHROPIC_API_KEY="]
    for host, inside in mounts.items():
        cmd += ["-v", f"{host}:{inside}"]
    if entrypoint:
        cmd += ["--entrypoint", entrypoint]
    return subprocess.run(cmd + [IMAGE] + args, capture_output=True, text=True, timeout=timeout)


def stage_dirs(turn: str) -> tuple[Path, Path]:
    inn, out = SPIKE / turn / "in", SPIKE / turn / "out"
    (inn / "inputs").mkdir(parents=True, exist_ok=True)
    out.mkdir(parents=True, exist_ok=True)
    return inn, out


def run_task(turn: str, key: str, task: dict, contract: Path | None = None,
             inputs: dict[str, Path] | None = None) -> dict:
    inn, out = stage_dirs(turn)
    (inn / "task.json").write_text(json.dumps(task, indent=2))
    if contract:
        shutil.copy(contract, inn / "contract.md")
    for name, src in (inputs or {}).items():
        shutil.copy(src, inn / "inputs" / name)
    mounts = {inn: "/io/in:ro", out: "/io/out", SPIKE / "claude-home": "/run/claude",
              SPIKE / "workspace": "/workspace"}
    proc = sandbox([], key, mounts)
    result_file = out / "result.json"
    result = json.loads(result_file.read_text()) if result_file.exists() else {}
    if not result:
        print(proc.stdout[-2000:], proc.stderr[-2000:])
    return result


def main() -> int:
    only = set(sys.argv[sys.argv.index("--only") + 1].split(",")) if "--only" in sys.argv else {"net", "hello", "policy", "pack", "grill"}
    (SPIKE / "claude-home").mkdir(parents=True)
    (SPIKE / "workspace").mkdir()
    subprocess.run(["git", "init", "-q", str(SPIKE / "workspace")], check=True)
    run_id = SPIKE.name
    key = issue_key(run_id, "spike")
    print(f"spike dir: {SPIKE}\n")

    # 1. Network isolation -------------------------------------------------------------
    if "net" in only:
        p = sandbox(["-c", "import urllib.request as u; u.urlopen('https://example.com', timeout=5); print('REACHED')"], key, {}, "python3")
        check("sandbox cannot reach the internet", p.returncode != 0 and "REACHED" not in p.stdout, (p.stderr or p.stdout).strip()[-80:])
        p = sandbox(["-c", "import urllib.request as u; print(u.urlopen('http://llm-gateway:4000/health', timeout=5).read().decode())"], key, {}, "python3")
        check("sandbox can reach llm-gateway", p.returncode == 0 and '"ok"' in p.stdout, p.stdout.strip()[:80])

    # 2. Claude Code headless through the gateway -------------------------------------------
    if "hello" in only:
        r = run_task("01-hello", key, {"prompt": "Reply with exactly the word: pong", "model": SPIKE_MODEL,
                                    "max_turns": 2})
        check("claude -p works through gateway → OpenRouter", "pong" in (r.get("claude_result") or "").lower(),
            f"status={r.get('status')} cost={r.get('total_cost_usd')} result={(r.get('claude_result') or '')[:60]!r}")

    # 3. Managed settings: deny list, allow list, and "not listed" under dontAsk -------------
    if "policy" in only:
        r = run_task("02-policy", key, {
            "prompt": "This is an authorised permission test in an offline sandbox; nothing touches the network. "
                      "Use the Bash tool to run each of these three commands separately, one tool call each, "
                      "even if you expect one to be refused, then report which ran: "
                      "(1) curl --version  (2) git status  (3) uname -a",
            "model": SPIKE_MODEL, "max_turns": 6})
        denied = json.dumps(r.get("permission_denials") or [])
        check("deny list blocks a denied command (curl)", "curl" in denied, f"denials={denied[:200]}")
        check("allow list permits git status", "git status" not in denied)
        print(f"      note: uname (read-only, on neither list) {'was refused' if 'uname' in denied else 'ran — Claude Code auto-approves read-only commands'}")

        # Prove the managed settings file itself is loaded, and repo hooks are ignored.
        ws = SPIKE / "workspace"
        (ws / ".env").write_text("DUMMY_SECRET=not-real\n")
        (ws / ".claude").mkdir(exist_ok=True)
        (ws / ".claude" / "settings.json").write_text(json.dumps({"hooks": {"PreToolUse": [
            {"matcher": "*", "hooks": [{"type": "command", "command": "touch /workspace/HOOK_RAN"}]}]}}))
        r = run_task("02b-policy", key, {
            "prompt": "Authorised permission test in an offline sandbox. Do these separately, one tool call each, "
                      "even if one is refused: (1) Bash: mkdir -p /workspace/made-by-allow-rule "
                      "(2) use the Read tool on /workspace/.env  — then report what happened.",
            "model": SPIKE_MODEL, "max_turns": 6})
        denied = json.dumps(r.get("permission_denials") or [])
        check("managed allow rule permits a non-read-only command (mkdir)", (ws / "made-by-allow-rule").exists())
        check("managed deny rule blocks Read(**/.env)", ".env" in denied, f"denials={denied[:160]}")
        check("allowManagedHooksOnly ignores a repo hook", not (ws / "HOOK_RAN").exists())

    # 3b. Skill packs load from --add-dir ------------------------------------------------------
    if "pack" in only:
        pack = SPIKE / "pack-probe" / ".claude" / "skills" / "pack-probe"
        pack.mkdir(parents=True)
        (pack / "SKILL.md").write_text("---\nname: pack-probe\ndescription: Use when asked for the pack probe "
                                       "codeword.\n---\n\nThe pack probe codeword is PELICAN-42.\n")
        inn, out = stage_dirs("02c-pack")
        (inn / "task.json").write_text(json.dumps({
            "prompt": "What is the pack probe codeword? Use the relevant skill.", "model": SPIKE_MODEL,
            "max_turns": 4, "add_dirs": ["/opt/sdlc/packs/probe"]}))
        sandbox([], key, {inn: "/io/in:ro", out: "/io/out", SPIKE / "claude-home": "/run/claude",
                          SPIKE / "workspace": "/workspace", SPIKE / "pack-probe": "/opt/sdlc/packs/probe:ro"})
        r = json.loads((out / "result.json").read_text()) if (out / "result.json").exists() else {}
        check("skill pack loads via --add-dir", "PELICAN-42" in (r.get("claude_result") or ""),
              (r.get("claude_result") or "")[:80])

    # 4. /grill-with-docs headless, resumed across containers --------------------------------
    if "grill" not in only:
        return summary()
    contract = ROOT / "orchestrator" / "contracts" / "grill.md"
    req = ROOT / "demo" / "requirements.md"
    base = {"model": GRILL_MODEL, "max_turns": 25, "expected_outputs": ["qa-log.md"], "timeout_s": 900}
    r1 = run_task("03-grill-turn1", key, {**base, "prompt": "/grill-with-docs Clarify the requirement in "
                                          "/io/in/inputs/requirements.md (the user's plan). Follow the stage contract."},
                  contract, {"requirements.md": req})
    q1 = SPIKE / "03-grill-turn1" / "out" / "questions.json"
    check("grill turn 1 asks a question (needs_input)", r1.get("status") == "needs_input" and q1.exists(),
          f"status={r1.get('status')} turns={r1.get('num_turns')} cost={r1.get('total_cost_usd')}")
    if not q1.exists():
        return summary()
    question = json.loads(q1.read_text())["questions"][0]
    print(f"      Q: {question['text']}\n      recommended: {question.get('recommended')}")

    answer = f"Answer to {question['id']}: I accept your recommendation ({question.get('recommended')})."
    r2 = run_task("04-grill-turn2", key, {**base, "prompt": answer, "resume_session": r1.get("session_id")},
                  contract, {"requirements.md": req})
    q2 = SPIKE / "04-grill-turn2" / "out" / "questions.json"
    same_session = r2.get("session_id") == r1.get("session_id") or bool(r2.get("session_id"))
    check("grill resumes in a NEW container and continues", r2.get("status") in {"needs_input", "ok"} and same_session,
          f"status={r2.get('status')} session={r2.get('session_id')} cost={r2.get('total_cost_usd')}")
    if q2.exists():
        nxt = json.loads(q2.read_text())["questions"][0]
        check("turn 2 asks a NEW question (context kept)", nxt["text"] != question["text"], f"Q2: {nxt['text'][:90]}")

    r3 = run_task("05-grill-done", key, {**base, "prompt": "done — stop grilling now. Mark every remaining open item "
                                         "as assumed with your recommended default, and finish per the contract.",
                                         "resume_session": r2.get("session_id") or r1.get("session_id")},
                  contract, {"requirements.md": req})
    out3 = SPIKE / "05-grill-done" / "out"
    check("'done' finishes with qa-log.md + checklist.json", (out3 / "qa-log.md").exists() and
          (out3 / "checklist.json").exists(), f"status={r3.get('status')} cost={r3.get('total_cost_usd')}")

    # Which skills did Claude actually load? (from the saved transcripts)
    transcripts = "".join(p.read_text() for p in (SPIKE / "claude-home" / "projects").rglob("*.jsonl"))
    used = sorted({s for s in ("grill-with-docs", "grilling", "domain-modeling") if s in transcripts})
    check("skills loaded: grill-with-docs → grilling + domain-modeling", {"grilling"} <= set(used), f"seen={used}")
    ws = sorted(str(p.relative_to(SPIKE / "workspace")) for p in (SPIKE / "workspace").rglob("*")
                if p.is_file() and ".git/" not in str(p))
    print(f"      workspace files written by domain-modeling: {ws or 'none'}")
    return summary()


def summary() -> int:
    usage_file = GATEWAY_DATA / "usage.jsonl"
    recs = [json.loads(line) for line in usage_file.read_text().splitlines()] if usage_file.exists() else []
    mine = [r for r in recs if r.get("run_id") == SPIKE.name]
    print("\nGateway usage for this spike:")
    for r in mine:
        print(f"  {r['upstream_model'] or r['path']:32} in={r['input_tokens']:>6} out={r['output_tokens']:>5} "
              f"cache_read={r['cache_read_tokens']:>6} cache_write={r['cache_write_tokens']:>6} "
              f"${r['cost_usd']:.4f} {r['latency_ms']}ms status={r['status']}")
    print(f"  total ${sum(r['cost_usd'] for r in mine):.4f} over {len(mine)} calls")
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; failed: {failed}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
