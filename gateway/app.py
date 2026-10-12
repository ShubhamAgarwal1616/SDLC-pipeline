"""Thin LLM gateway (PLAN.md §25.1).

Claude Code in the sandbox talks only to this service. It:
  1. accepts a per-run pipeline key and checks it against keys.json (written by the orchestrator),
  2. stops the call if that key's budget is spent,
  3. swaps in the real provider key and maps canonical model ids to provider ids,
  4. forwards to the provider (OpenRouter's Anthropic-compatible API) and streams the reply back,
  5. records tokens, cost and latency per call in usage.jsonl.
The provider key never leaves this container.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import time
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

UPSTREAM = os.environ.get("UPSTREAM_BASE", "https://openrouter.ai/api").rstrip("/")
UPSTREAM_KEY = os.environ["OPENROUTER_API_KEY"]
DATA = Path(os.environ.get("GATEWAY_DATA", "/data"))
KEYS_FILE = DATA / "keys.json"
USAGE_FILE = DATA / "usage.jsonl"

MODEL_MAP = {
    "claude-sonnet-5-5": "anthropic/claude-sonnet-5.5",
    "claude-opus-5-5": "anthropic/claude-opus-5.5",
    "claude-haiku-4-5-20251001": "anthropic/claude-haiku-4.5",
    **json.loads(os.environ.get("MODEL_MAP_JSON") or "{}"),   # SDLC_MODEL_MAP_JSON in .env
}
PASS_HEADERS = {"anthropic-version", "anthropic-beta", "content-type", "accept"}

client = httpx.AsyncClient(timeout=httpx.Timeout(600, connect=10))
pricing: dict[str, dict[str, float]] = {}
spent: dict[str, float] = {}


async def load_pricing() -> None:
    try:
        resp = await client.get(f"{UPSTREAM}/v1/models")
        for model in resp.json().get("data", []):
            p = model.get("pricing") or {}
            pricing[model["id"]] = {k: float(p.get(k) or 0) for k in
                                    ("prompt", "completion", "input_cache_read", "input_cache_write")}
    except Exception as exc:  # pricing is best effort; usage is still recorded
        print(f"gateway: could not load pricing: {exc}", flush=True)


def load_spent() -> None:
    if USAGE_FILE.exists():
        for line in USAGE_FILE.read_text().splitlines():
            rec = json.loads(line)
            spent[rec["key_id"]] = spent.get(rec["key_id"], 0.0) + rec.get("cost_usd", 0.0)


@asynccontextmanager
async def lifespan(_: FastAPI):
    DATA.mkdir(parents=True, exist_ok=True)
    await load_pricing()
    load_spent()
    yield
    await client.aclose()


app = FastAPI(lifespan=lifespan)


def upstream_model(model: str) -> str:
    if model in MODEL_MAP:
        return MODEL_MAP[model]
    if "/" in model:
        return model
    base = re.sub(r"-\d{8}$", "", model)                  # drop a date suffix
    base = re.sub(r"-(\d+)-(\d+)$", r"-\1.\2", base)      # claude-x-5-5 → claude-x-5.5
    return f"anthropic/{base}"


def error(status: int, kind: str, message: str) -> JSONResponse:
    return JSONResponse({"type": "error", "error": {"type": kind, "message": message}}, status_code=status)


def record(meta: dict, usage: dict, started: float) -> None:
    price = pricing.get(meta.get("upstream_model") or "", {})
    tokens = {
        "input_tokens": int(usage.get("input_tokens") or 0),
        "output_tokens": int(usage.get("output_tokens") or 0),
        "cache_read_tokens": int(usage.get("cache_read_input_tokens") or 0),
        "cache_write_tokens": int(usage.get("cache_creation_input_tokens") or 0),
    }
    cost = (tokens["input_tokens"] * price.get("prompt", 0) + tokens["output_tokens"] * price.get("completion", 0)
            + tokens["cache_read_tokens"] * price.get("input_cache_read", 0)
            + tokens["cache_write_tokens"] * price.get("input_cache_write", 0))
    rec = {"at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), **meta, **tokens,
           "cost_usd": round(cost, 6), "latency_ms": int((time.monotonic() - started) * 1000)}
    spent[meta["key_id"]] = spent.get(meta["key_id"], 0.0) + rec["cost_usd"]
    with USAGE_FILE.open("a") as fh:
        fh.write(json.dumps(rec) + "\n")


@app.get("/health")
async def health() -> dict:
    return {"ok": True, "upstream": UPSTREAM, "priced_models": len(pricing)}


@app.api_route("/v1/{path:path}", methods=["GET", "POST"])
async def proxy(path: str, request: Request):
    auth = request.headers.get("authorization", "")
    token = auth[7:].strip() if auth.lower().startswith("bearer ") else request.headers.get("x-api-key", "")
    keys = json.loads(KEYS_FILE.read_text()) if KEYS_FILE.exists() else {}
    entry = keys.get(token)
    if not token or entry is None:
        return error(401, "authentication_error", "unknown or revoked pipeline key")
    key_id = hashlib.sha256(token.encode()).hexdigest()[:12]
    budget = entry.get("budget_usd")
    if budget is not None and spent.get(key_id, 0.0) >= budget:
        return error(403, "permission_error",
                     f"pipeline budget exhausted for {entry.get('run_id')} ({spent[key_id]:.4f} of {budget} USD)")

    body = await request.body()
    requested = mapped = None
    if body and "application/json" in request.headers.get("content-type", ""):
        payload = json.loads(body)
        if "model" in payload:
            requested = payload["model"]
            mapped = payload["model"] = upstream_model(requested)
            body = json.dumps(payload).encode()

    headers = {k: v for k, v in request.headers.items() if k.lower() in PASS_HEADERS}
    headers.update({"authorization": f"Bearer {UPSTREAM_KEY}", "accept-encoding": "identity",
                    "x-title": "sdlc-pipeline"})
    started = time.monotonic()
    upstream = await client.send(
        client.build_request(request.method, f"{UPSTREAM}/v1/{path}", content=body, headers=headers,
                             params=request.query_params), stream=True)
    meta = {"key_id": key_id, "run_id": entry.get("run_id"), "stage": entry.get("stage"), "path": path,
            "model": requested, "upstream_model": mapped, "status": upstream.status_code}
    out_headers = {k: v for k, v in upstream.headers.items() if k.lower() in {"content-type", "request-id"}}

    if "text/event-stream" in upstream.headers.get("content-type", ""):
        async def relay():
            usage: dict = {}
            buffer = b""
            try:
                async for chunk in upstream.aiter_bytes():
                    yield chunk
                    buffer += chunk
                    while b"\n" in buffer:
                        line, buffer = buffer.split(b"\n", 1)
                        if not line.startswith(b"data:"):
                            continue
                        try:
                            event = json.loads(line[5:].strip())
                        except ValueError:
                            continue
                        if event.get("type") == "message_start":
                            usage.update((event.get("message") or {}).get("usage") or {})
                        elif event.get("type") == "message_delta":
                            usage.update(event.get("usage") or {})
            finally:
                await upstream.aclose()
                record(meta, usage, started)
        return StreamingResponse(relay(), status_code=upstream.status_code, headers=out_headers)

    content = await upstream.aread()
    await upstream.aclose()
    try:
        usage = json.loads(content).get("usage") or {}
    except (ValueError, AttributeError):
        usage = {}
    if upstream.status_code >= 400:
        meta["error"] = content[:500].decode(errors="replace")
    record(meta, usage, started)
    return Response(content, status_code=upstream.status_code, headers=out_headers)
