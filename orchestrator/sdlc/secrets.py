"""Phase 1 secrets: read from .env (or the environment). Never logged, never passed to the sandbox."""

from __future__ import annotations

import os
import re
from functools import lru_cache
from pathlib import Path

from sdlc.config import HOME


class MissingSecret(Exception):
    pass


@lru_cache(maxsize=1)
def _dotenv() -> dict[str, str]:
    path = HOME / ".env"
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                value = value.strip()
                if value[:1] in {'"', "'"}:
                    value = value[1:].split(value[0], 1)[0]
                else:
                    value = re.split(r"(?:^|\s+)#", value, maxsplit=1)[0].strip()   # inline comment
                values[key.strip()] = value
    return values


def get(name: str, required: bool = True) -> str | None:
    value = os.environ.get(name) or _dotenv().get(name)
    if required and not value:
        raise MissingSecret(f"{name} is not set (add it to .env)")
    return value


def names() -> set[str]:
    """Every variable name set in .env or the environment (names only)."""
    return set(_dotenv()) | set(os.environ)


def save(values: dict[str, str]) -> Path:
    """Set or replace NAME=value lines in the home .env (created with mode 600). Values are never printed."""
    path = HOME / ".env"
    path.parent.mkdir(parents=True, exist_ok=True)
    lines = path.read_text().splitlines() if path.exists() else []
    keep = [ln for ln in lines if ln.split("=", 1)[0].strip() not in values]
    keep += [f"{k}={v}" for k, v in values.items()]
    path.touch(mode=0o600)
    path.chmod(0o600)
    path.write_text("\n".join(keep) + "\n")
    _dotenv.cache_clear()
    return path
