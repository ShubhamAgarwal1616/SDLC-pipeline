"""Local Docker plumbing without docker compose or local builds: `sdlc up` / `sdlc down`.

Pulls the prebuilt worker and gateway images, creates the two networks (sdlc_internal is internal: the
sandbox's only route is the gateway) and starts the gateway. The sandbox runner calls `ensure()` before a
stage, so a fresh machine or CI runner needs no setup step. The provider key reaches the gateway container
through the environment of `docker run` (never its command line).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request

from sdlc import secrets
from sdlc.config import RUNS, EffectiveConfig, expand

GATEWAY = "llm-gateway"
EGRESS = "sdlc_egress"


class InfraError(Exception):
    pass


def _docker(*args: str, env: dict | None = None, check: bool = False) -> subprocess.CompletedProcess:
    try:
        proc = subprocess.run(["docker", *args], capture_output=True, text=True, env=env)
    except FileNotFoundError as exc:
        raise InfraError("docker isn't installed or isn't on PATH") from exc
    if check and proc.returncode != 0:
        raise InfraError(f"docker {args[0]} failed: {proc.stderr.strip()[-400:]}")
    return proc


def user_args() -> list[str]:
    """On Linux, bind mounts keep host ownership: run containers as the host user so both sides can write
    (Docker Desktop on macOS maps ownership itself). HOME moves to /tmp because that uid has no home."""
    if sys.platform.startswith("linux") and os.getuid() != 0:
        return ["--user", f"{os.getuid()}:{os.getgid()}", "-e", "HOME=/tmp"]
    return []


def images(cfg: EffectiveConfig) -> dict[str, str]:
    return {"worker": expand(cfg.get("sandbox.image", "sdlc-worker:dev")),
            "gateway": expand(cfg.get("sandbox.gateway_image", "sdlc-gateway:dev"))}


def ensure_image(image: str) -> None:
    if _docker("image", "inspect", image).returncode == 0:
        return
    pulled = _docker("pull", "--quiet", image)
    if pulled.returncode != 0:
        hint = (" For a local build set SDLC_WORKER_IMAGE=sdlc-worker:dev / SDLC_GATEWAY_IMAGE=sdlc-gateway:dev in .env."
                if ":dev" not in image else " Build it: docker build -f worker/Dockerfile -t sdlc-worker:dev .")
        raise InfraError(f"image {image} isn't available ({pulled.stderr.strip()[-200:]}).{hint}")


def ensure_networks(internal: str) -> None:
    for name, extra in ((internal, ["--internal"]), (EGRESS, [])):
        if _docker("network", "inspect", name).returncode != 0:
            _docker("network", "create", *extra, name, check=True)


def healthy(url: str, timeout: float = 3) -> dict | None:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return json.loads(resp.read())
    except Exception:
        return None


def gateway_up(cfg: EffectiveConfig, recreate: bool = False) -> str:
    """Start the gateway unless a healthy one is already running (e.g. the docker compose one in a checkout)."""
    health_url = cfg.get("sandbox.gateway_health_url", "http://127.0.0.1:4000/health")
    if not recreate and healthy(health_url):
        return "running"
    image = images(cfg)["gateway"]
    ensure_image(image)
    _docker("rm", "-f", GATEWAY)
    data = RUNS / "_gateway"
    data.mkdir(parents=True, exist_ok=True)
    key = secrets.get("OPENROUTER_API_KEY")
    env = {**os.environ, "OPENROUTER_API_KEY": key,
           "MODEL_MAP_JSON": secrets.get("SDLC_MODEL_MAP_JSON", required=False) or ""}
    _docker("run", "-d", "--name", GATEWAY, "--restart", "unless-stopped", "--network", EGRESS,
            "-p", "127.0.0.1:4000:4000", *user_args(),
            "-e", "OPENROUTER_API_KEY", "-e", "MODEL_MAP_JSON", "-e", "UPSTREAM_BASE=https://openrouter.ai/api",
            "-v", f"{data}:/data", image, env=env, check=True)
    _docker("network", "connect", "--alias", GATEWAY, cfg.get("sandbox.docker_network", "sdlc_internal"), GATEWAY,
            check=True)
    for _ in range(40):
        if healthy(health_url):
            return "started"
        time.sleep(0.5)
    logs = _docker("logs", "--tail", "20", GATEWAY).stderr
    raise InfraError(f"the gateway didn't become healthy at {health_url}: {logs.strip()[-400:]}")


def ensure(cfg: EffectiveConfig, need_gateway: bool = True) -> None:
    """Everything a stage needs: networks, the worker image and (for LLM stages) a healthy gateway."""
    ensure_networks(cfg.get("sandbox.docker_network", "sdlc_internal"))
    ensure_image(images(cfg)["worker"])
    if need_gateway:
        gateway_up(cfg)


def down() -> None:
    _docker("rm", "-f", GATEWAY)
