
---
name: sdlc-pipeline-decisions
description: "Decisions made for the SDLC_workflow agentic pipeline project (repo location, SCM, demo service, spec approach, LLM provider, runtime)"
metadata:
  node_type: memory
  type: project
  originSessionId: c524d695-d3c2-4efc-89c4-5668ed07c8a8
  modified: 2026-10-12T09:16:04.616Z
---
Decisions for the agentic SDLC pipeline in `AI_Projects/SDLC_workflow` (made 2026-10-12):

- Lives as a subfolder of the existing AI_Projects git repo (not its own repo).
- SCM target: **GitHub** first (PRs); GitLab adapter possible later.
- Demo target service: small **Kotlin/Http4k** service under `demo/sample-repo` (exercises the user's kotlin-service-standards + tdd skills).
- A2/A3: **chain `to-prd` + OpenSpec** (PRD → Confluence; OpenSpec change folders drive Jira, validation, review).
- LLM provider: **OpenRouter**, reached only via a thin Phase 1 gateway proxy; sandbox has no internet and no provider key.
- All LLM calls via **Claude Code headless** (`claude -p`) in Phase 2; entry skill named in prompt (`/grill-with-docs`, `/tdd`), others auto-picked.
- Verification runs in a separate no-network verify container, never in Phase 1.

**Why:** chosen by the user while planning; design docs are PLAN.md, BUILD_AND_ROLLOUT.md, sdlc-pipeline-flow.html.
**How to apply:** build to these choices; don't re-ask. Related: [[sdlc-pipeline-user-skills]]
