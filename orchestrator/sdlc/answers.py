"""The answers.md channel for grill questions (PLAN.md §20.1).

The pipeline writes answers.md; a person fills it in; parse() turns it into answers.json.
Empty answer → accept the default. "skip" → undecided (becomes an open assumption).
"""

from __future__ import annotations

from typing import Any

HEADER = """# Questions for {run_id} · round {round}
Write your answer under each question.
Leave "Answer:" empty to accept the default. Write "skip" if nobody knows yet.
"""


def render(run_id: str, round_no: int, questions: list[dict[str, Any]]) -> str:
    parts = [HEADER.format(run_id=run_id, round=round_no)]
    for q in questions:
        audience = f"  (for: {q['audience']})" if q.get("audience") else ""
        parts.append(f"## {q['id']} · {q['text']}{audience}\n"
                     f"Default: {q.get('default') or '(none)'}\n"
                     f"Answer:\n")
    return "\n".join(parts)


def _answer_blocks(text: str) -> dict[str, str]:
    blocks: dict[str, str] = {}
    current: str | None = None
    capturing = False
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if current is not None:
                blocks[current] = "\n".join(lines).strip()
            current = line[3:].split("·", 1)[0].strip()
            capturing, lines = False, []
        elif current is not None and line.strip().startswith("Answer:"):
            capturing = True
            lines = [line.strip()[len("Answer:"):]]
        elif capturing:
            lines.append(line)
    if current is not None:
        blocks[current] = "\n".join(lines).strip()
    return blocks


def _resolve(q: dict[str, Any], raw: str) -> dict[str, Any]:
    if raw.lower() == "skip":
        return {"id": q["id"], "value": None, "source": "skipped"}
    if raw:
        return {"id": q["id"], "value": raw, "source": "human"}
    return {"id": q["id"], "value": q.get("default"), "source": "default"}


def parse(text: str, questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    blocks = _answer_blocks(text)
    return [_resolve(q, blocks.get(q["id"], "")) for q in questions]


def from_values(questions: list[dict[str, Any]], values: dict[str, str]) -> list[dict[str, Any]]:
    return [_resolve(q, (values.get(q["id"]) or "").strip()) for q in questions]


def defaults(questions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [_resolve(q, "") for q in questions]
