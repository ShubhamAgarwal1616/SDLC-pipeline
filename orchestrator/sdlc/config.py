"""Layered pipeline config: org defaults → project .sdlc/pipeline.yaml → run overrides.

Lower layers may tighten but not loosen keys listed under `locks` in the org file
(PLAN.md §15). Every leaf value remembers which layer set it, for `sdlc config --explain`.
"""

from __future__ import annotations

import copy
import fnmatch
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

PACKAGE_DIR = Path(__file__).resolve().parent
_BUNDLE = PACKAGE_DIR / "_assets"          # an installed wheel carries config/, contracts, skills, evals here
CHECKOUT = PACKAGE_DIR.parents[1]
INSTALLED = _BUNDLE.is_dir()
# Read-only assets (org config, stage contracts, skills, evals), with the same layout as a source checkout.
ROOT = _BUNDLE if INSTALLED else CHECKOUT
# Mutable state: .env, runs/, config/repos.yaml. A checkout keeps it next to the code (as before);
# an installed CLI uses ~/.sdlc. SDLC_HOME overrides both (CI sets it).
HOME = Path(os.environ.get("SDLC_HOME") or (Path.home() / ".sdlc" if INSTALLED else CHECKOUT)).expanduser()
RUNS = HOME / "runs"
ORG_CONFIG_DEFAULT = ROOT / "config" / "org.yaml"
PROJECT_CONFIG_REL = Path(".sdlc") / "pipeline.yaml"

LOCK_RULES = {"fixed", "max", "one_of", "append_only"}


class ConfigError(Exception):
    """Invalid config, or a lower layer tried to loosen a locked key."""


@dataclass(frozen=True)
class Lock:
    key: str
    rule: str
    ref: str | None = None


def get_path(data: dict, key: str, default: Any = None) -> Any:
    node: Any = data
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node


def flatten(data: dict, prefix: str = "") -> dict[str, Any]:
    """Dotted leaf paths → values. Lists are leaves."""
    out: dict[str, Any] = {}
    for key, value in data.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and value:
            out.update(flatten(value, path + "."))
        else:
            out[path] = value
    return out


def unflatten(pairs: dict[str, Any]) -> dict:
    out: dict = {}
    for key, value in pairs.items():
        node = out
        *parents, leaf = key.split(".")
        for part in parents:
            node = node.setdefault(part, {})
        node[leaf] = value
    return out


def _merge(base: dict, prov: dict[str, str], layer: dict, layer_name: str,
           append_only: set[str], prefix: str = "") -> None:
    for key, value in layer.items():
        path = f"{prefix}{key}"
        if isinstance(value, dict) and isinstance(base.get(key), dict):
            _merge(base[key], prov, value, layer_name, append_only, path + ".")
            continue
        if path in append_only and isinstance(base.get(key), list) and isinstance(value, list):
            base[key] = base[key] + [v for v in value if v not in base[key]]
        else:
            base[key] = copy.deepcopy(value)
        for stale in [p for p in prov if p == path or p.startswith(path + ".")]:
            del prov[stale]
        leaves = flatten({key: base[key]}, prefix) if isinstance(base[key], dict) and base[key] else {path: base[key]}
        for leaf in leaves:
            prov[leaf] = layer_name


def _parse_locks(org: dict) -> list[Lock]:
    locks = []
    for raw in org.get("locks", []) or []:
        lock = Lock(key=raw["key"], rule=raw["rule"], ref=raw.get("ref"))
        if lock.rule not in LOCK_RULES:
            raise ConfigError(f"org lock on {lock.key}: unknown rule {lock.rule!r}")
        if lock.rule in {"max", "one_of"} and not lock.ref:
            raise ConfigError(f"org lock on {lock.key}: rule {lock.rule!r} needs a ref")
        locks.append(lock)
    return locks


def _matching(pattern: str, *trees: dict) -> set[str]:
    keys: set[str] = set()
    for tree in trees:
        keys.update(k for k in flatten(tree) if fnmatch.fnmatchcase(k, pattern))
    return keys


def _check_locks(merged: dict, org: dict, prov: dict[str, str], locks: list[Lock]) -> list[str]:
    errors = []
    for lock in locks:
        for key in sorted(_matching(lock.key, merged, org)):
            value, org_value = get_path(merged, key), get_path(org, key)
            who = prov.get(key, "org")
            if lock.rule == "fixed" and value != org_value:
                errors.append(f"{key} is locked by org to {org_value!r}; {who} set {value!r}")
            elif lock.rule == "max":
                ceiling = get_path(org, lock.ref)
                if value is not None and ceiling is not None and value > ceiling:
                    errors.append(f"{key}={value!r} ({who}) exceeds the org ceiling {lock.ref}={ceiling!r}")
            elif lock.rule == "one_of":
                allowed = get_path(org, lock.ref) or []
                if value not in allowed:
                    errors.append(f"{key}={value!r} ({who}) is not in {lock.ref} {allowed}")
            elif lock.rule == "append_only":
                missing = [v for v in (org_value or []) if v not in (value or [])]
                if missing:
                    errors.append(f"{key} can only be added to; {who} removed {missing}")
    return errors


def env_models(org: dict, env: dict[str, str] | None = None) -> dict[str, Any]:
    """Operator settings from .env (or the environment), as dotted overrides of the org layer.

    SDLC_MODEL_DEFAULT       every stage under llm.stages
    SDLC_MODEL_<STAGE>       one stage, e.g. SDLC_MODEL_REVIEW, SDLC_MODEL_RESOLVE_COMMENTS (wins over DEFAULT)
    SDLC_MODEL_REVIEW_FOLLOWUP  review rounds 2+ (llm.stages.review.followup_model)
    SDLC_ALLOWED_MODELS      comma-separated allow-list, replacing llm.allowed_models
    SDLC_WORKER_IMAGE        sandbox.image (e.g. sdlc-worker:dev for a local build)
    SDLC_GATEWAY_IMAGE       sandbox.gateway_image
    Models named here are added to the allow-list: .env is the operator's own setting, like org.yaml.
    """
    if env is None:
        from sdlc import secrets           # lazy: secrets imports this module
        env = {k: v for k in secrets.names() if (v := secrets.get(k, required=False))}
    out: dict[str, Any] = {}
    default = env.get("SDLC_MODEL_DEFAULT", "").strip()
    for stage in get_path(org, "llm.stages", {}) or {}:
        model = env.get(f"SDLC_MODEL_{stage.upper()}", "").strip() or default
        if model:
            out[f"llm.stages.{stage}.model"] = model
    if followup := env.get("SDLC_MODEL_REVIEW_FOLLOWUP", "").strip():
        out["llm.stages.review.followup_model"] = followup
    allowed = [m.strip() for m in env.get("SDLC_ALLOWED_MODELS", "").split(",") if m.strip()]
    if allowed or out:
        base = allowed or list(get_path(org, "llm.allowed_models", []) or [])
        out["llm.allowed_models"] = base + [m for m in dict.fromkeys(out.values()) if m not in base]
    for name, key in (("SDLC_WORKER_IMAGE", "sandbox.image"), ("SDLC_GATEWAY_IMAGE", "sandbox.gateway_image")):
        if env.get(name, "").strip():
            out[key] = env[name].strip()
    return out


def version() -> str:
    from importlib.metadata import PackageNotFoundError, version as pkg_version
    try:
        return pkg_version("sdlc-pipeline")
    except PackageNotFoundError:
        return "0.0.0"


def expand(value: str) -> str:
    """Fill {version} in image refs and install sources."""
    return value.replace("{version}", version())


def _org_layer(org_path: Path, env: dict[str, str] | None) -> tuple[dict, dict[str, str], list[Lock]]:
    """org.yaml with the .env model settings applied: (org data without locks, provenance, locks)."""
    raw = _read_yaml(org_path)
    locks = _parse_locks(raw)
    org = {k: copy.deepcopy(v) for k, v in raw.items() if k != "locks"}
    prov = {k: "org" for k in flatten(org)}
    from_env = env_models(org, env)
    if from_env:
        _merge(org, prov, unflatten(from_env), "env (.env)", set())
    return org, prov, locks


INSTRUCTIONS_REL = Path(".sdlc") / "instructions"
INSTRUCTION_STAGES = ("grill", "propose", "implement", "review", "fix")   # = contract names
MAX_INSTRUCTIONS_BYTES = 8000


def project_instructions(project_dir: Path) -> dict[str, str]:
    """A repo's extra rules per LLM stage: .sdlc/instructions/<stage>.md, appended to that stage's contract.
    Read from the project snapshot (the base branch) when a run starts, so they're frozen with its config."""
    out = {}
    for stage in INSTRUCTION_STAGES:
        path = project_dir / INSTRUCTIONS_REL / f"{stage}.md"
        if path.is_file() and not path.is_symlink():
            text = path.read_text(errors="replace").strip()
            if len(text.encode()) > MAX_INSTRUCTIONS_BYTES:
                raise ConfigError(f"{path}: over {MAX_INSTRUCTIONS_BYTES} bytes; keep stage instructions short")
            if text:
                out[stage] = text
    return out


def _read_yaml(path: Path) -> dict:
    try:
        data = yaml.safe_load(path.read_text()) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ConfigError(f"{path}: top level must be a mapping")
    return data


def parse_overrides(pairs: list[str]) -> dict[str, Any]:
    """`key=value` strings (value parsed as YAML) → dotted dict."""
    out: dict[str, Any] = {}
    for pair in pairs:
        if "=" not in pair:
            raise ConfigError(f"override {pair!r} must look like key=value")
        key, raw = pair.split("=", 1)
        out[key.strip()] = yaml.safe_load(raw)
    return out


@dataclass
class EffectiveConfig:
    data: dict
    provenance: dict[str, str]

    def get(self, key: str, default: Any = None) -> Any:
        return get_path(self.data, key, default)

    def explain(self, key: str) -> list[tuple[str, Any, str]]:
        leaves = {k: v for k, v in flatten(self.data).items() if k == key or k.startswith(key + ".")}
        if not leaves:
            raise ConfigError(f"no config key {key!r}")
        return [(k, v, self.provenance.get(k, "org")) for k, v in sorted(leaves.items())]

    def to_json(self) -> str:
        return json.dumps({"data": self.data, "provenance": self.provenance}, indent=2)

    @classmethod
    def from_json(cls, text: str) -> "EffectiveConfig":
        raw = json.loads(text)
        return cls(data=raw["data"], provenance=raw["provenance"])


def with_overrides(cfg: EffectiveConfig, overrides: dict[str, Any], layer_name: str = "run (resume)",
                   org_path: Path | None = None, env: dict[str, str] | None = None) -> EffectiveConfig:
    """Apply more run overrides to a frozen config (e.g. raising a budget on resume), still under org locks."""
    org_path = org_path or Path(os.environ.get("SDLC_ORG_CONFIG", ORG_CONFIG_DEFAULT))
    org, _, locks = _org_layer(org_path, env)
    # A frozen run keeps the allow-list it started with, even if .env changed since.
    if cfg.get("llm.allowed_models") is not None:
        org.setdefault("llm", {})["allowed_models"] = cfg.get("llm.allowed_models")
    merged, prov = copy.deepcopy(cfg.data), dict(cfg.provenance)
    _merge(merged, prov, unflatten(overrides), layer_name, {lk.key for lk in locks if lk.rule == "append_only"})
    errors = _check_locks(merged, org, prov, locks)
    if errors:
        raise ConfigError("config breaks org locks:\n  - " + "\n  - ".join(errors))
    return EffectiveConfig(data=merged, provenance=prov)


def load_config(project_dir: Path | None = None, overrides: dict[str, Any] | None = None,
                org_path: Path | None = None, env: dict[str, str] | None = None) -> EffectiveConfig:
    """`env` replaces .env/os.environ for the model settings (tests pass {} to stay hermetic)."""
    org_path = org_path or Path(os.environ.get("SDLC_ORG_CONFIG", ORG_CONFIG_DEFAULT))
    org, prov, locks = _org_layer(org_path, env)
    append_only = {lock.key for lock in locks if lock.rule == "append_only"}
    merged = copy.deepcopy(org)

    layers: list[tuple[str, dict]] = []
    if project_dir is not None:
        project_file = Path(project_dir) / PROJECT_CONFIG_REL
        if project_file.exists():
            layers.append((f"project ({project_file})", _read_yaml(project_file)))
        if extra := project_instructions(Path(project_dir)):
            layers.append((f"project ({INSTRUCTIONS_REL}/)", {"instructions": extra}))
    if overrides:
        layers.append(("run", unflatten(overrides)))

    for name, layer in layers:
        if "locks" in layer:
            raise ConfigError(f"{name}: only the org config may define locks")
        _merge(merged, prov, layer, name, append_only)

    errors = _check_locks(merged, org, prov, locks)
    if errors:
        raise ConfigError("config breaks org locks:\n  - " + "\n  - ".join(errors))
    return EffectiveConfig(data=merged, provenance=prov)
