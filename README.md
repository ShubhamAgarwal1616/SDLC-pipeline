# SDLC pipeline

A two-phase agentic pipeline. It turns a requirements file into clarified specs and Jira stories, then turns a story into a reviewed pull request.

- **Continuing the build? Start with [HANDOFF.md](HANDOFF.md).**
- Design: [PLAN.md](PLAN.md)
- Build order: [BUILD_AND_ROLLOUT.md](BUILD_AND_ROLLOUT.md)
- Diagrams and examples: [sdlc-pipeline-flow.html](sdlc-pipeline-flow.html)

**Status:**
- **P1 (walking skeleton) is done.** Every stage is a fake, so the whole flow runs without Docker, an LLM, or Jira and GitHub.
- **P2 (sandbox and LLM gateway) is done.** The real Claude Code sandbox and the OpenRouter gateway are proven by `scripts/p2_spike.py`.
- **P3 (requirements loop) is done.** `sdlc plan` runs real intake, grill (`/grill-with-docs`), propose (`/to-prd` → `/openspec-propose`) and validate stages. Stages that aren't built yet are faked automatically, and the CLI says which ones.
- **P5 and P6 are done: the full flow works end to end** (Demo 3: KAN-8 went from Jira story to merged, reviewed PR to Done for $2.94). `sdlc implement KEY --repo …` fetches the story, runs `/tdd`, verifies in a no-network container, gets an Opus review, applies the diff guard, and opens a PR. `/sdlc fix`, `/sdlc approve` and merge go through `sdlc watch`.
- **P7–P10 are done** (P10's live demo is pending):
  - P7 observability: `report --all`, `dashboard`, spans, budget warnings
  - P8 hardening: offline semgrep, red-team cases and evals via `sdlc eval`
  - P9 ops: `doctor`, `skills`, reminders, sync bundles, `watch --launchd`
  - P10 multi-repo: `plan --repos`, `implement --feature`
  - User guide: `docs/USER_GUIDE.md`
- **P4 (publish) is done.** `sdlc init --repo`, a spec PR per run, `/sdlc revise` and merge via `sdlc watch`, then a Jira Epic and Stories. Confluence is faked.
- **Packaging + GitHub Actions mode (0.2.0, not yet live-tested):** developers install the CLI with `uv tool install`, run `sdlc init` in their repo, and then open issues. The pipeline runs in the repo's Actions on prebuilt GHCR images (`.github/workflows/release.yml` publishes them). See **Using it in another repo** below and `docs/USER_GUIDE.md`.

## Using it in another repo (no clone, no Docker builds)

```bash
uv tool install git+https://github.com/shubhamagarwal1616/sdlc-pipeline@v0.2.0   # once per machine
cd my-service && sdlc init --jira-project KAN                                       # once per repo
# then open an issue with the "Feature request (SDLC)" template
```
Releasing a version (pipeline maintainers): bump `version` in `pyproject.toml`, tag `vX.Y.Z` and push the tag.
`release.yml` tests it and pushes `ghcr.io/<owner>/sdlc-worker` and `sdlc-gateway` for amd64 and arm64. Make both
GHCR packages **public** (or grant the target repos read access) so workflows can pull them.

## Developing the pipeline (this checkout)

```bash
uv sync                      # Python 3.12+, installs the `sdlc` CLI
uv run pytest -q             # engine, config and answers tests
cp .env.example .env         # fill in yourself; needed from P2 (OpenRouter key). Stage models: SDLC_MODEL_* (docs/USER_GUIDE.md#choosing-models)
# local image builds: set SDLC_WORKER_IMAGE=sdlc-worker:dev and SDLC_GATEWAY_IMAGE=sdlc-gateway:dev in .env
```

## Try it (fake stages)

```bash
# Pipeline A: requirements → spec → tickets
uv run sdlc plan demo/requirements.md --fake
uv run sdlc answer <run-id>                     # interactive; Enter accepts the default
#   or fill in runs/<run-id>/answers.md, then: uv run sdlc answer <run-id> --file runs/<run-id>/answers.md
uv run sdlc review <run-id> --changes "split burst handling out"   # loops back to propose
uv run sdlc review <run-id> --approve

# Pipeline B: story → PR. Force failures to see the feedback loops.
uv run sdlc implement TODO-42 --fake --fake-fail b4_verify=2 --fake-fail b5_review=1
uv run sdlc inspect <run-id>
uv run sdlc review <run-id> --changes "exempt /health"     # PR comment → fix → verify → review
uv run sdlc review <run-id> --approve

# Run control and config
uv run sdlc status
uv run sdlc resume <run-id> --from b3_implement    # after an escalation
uv run sdlc config --explain loops                 # which layer set each value
```

Run state lives in `runs/<run-id>/`, which git ignores:

| File | Contents |
|---|---|
| `state.json` | Where the run is |
| `config.json` | The config, frozen for this run |
| `events.jsonl` | Audit log |
| `answers.md` | The grill questions to fill in |
| `artifacts/<stage>/attempt-N/` | What each stage produced, per attempt |

## Real run (P4): requirements → spec PR → Jira

```bash
uv run sdlc init --repo owner/repo --jira-project KEY        # once per repo
uv run sdlc plan demo/requirements.md --repo owner/repo       # grill → propose → validate → spec PR
uv run sdlc watch                                             # keep running: /sdlc revise, merge → Jira
```

## Real run (P3): requirements → validated OpenSpec changes

```bash
docker compose up -d                                   # gateway (needs OPENROUTER_API_KEY in .env)
uv run sdlc plan demo/requirements.md                  # intake + first grill question (~20 s)
uv run sdlc answer <run-id>                            # or --file runs/<run-id>/answers.md, --accept-defaults, --done
uv run sdlc review <run-id> --changes "…"              # revises the change folders; or --approve
uv run sdlc resume <run-id> --set budgets.per_stage_usd.propose=1.5   # after a budget stop
uv run sdlc report <run-id>                            # tokens and $ per stage
```

The run's repo (PRD, `CONTEXT.md`, `openspec/changes/…`) is in `runs/<run-id>/workspace/`.

## P2: sandbox and gateway

```bash
docker compose up -d                                   # thin LLM gateway → OpenRouter (reads OPENROUTER_API_KEY from .env)
docker build -f worker/Dockerfile -t sdlc-worker:dev . # Claude Code + OpenSpec + skills/
uv run python scripts/p2_spike.py                      # isolation, policy, packs, 3-turn grill (~$0.15)
uv run python scripts/p2_spike.py --only policy        # or: net,hello,policy,pack,grill
```

Gateway usage per call (tokens, cost, latency) is logged to `runs/_gateway/usage.jsonl`.

## Layout

```
config/org.yaml            org defaults + locks (layer 1 of the config)
orchestrator/sdlc/
  cli.py                   the `sdlc` command
  config.py                layered config, locks, provenance
  answers.py               answers.md render/parse
  engine/                  state, stage interface, transitions table, runner
  stages/                  fake stages now; real ones replace them phase by phase
orchestrator/tests/
gateway/                   thin LLM gateway (per-run keys, budgets, usage log) → OpenRouter
worker/                    Phase 2 image: Dockerfile, entrypoint, managed settings, org CLAUDE.md
orchestrator/contracts/    stage contracts (pipeline-mode instructions), e.g. grill.md
scripts/p2_spike.py        the P2 verification script
skills/                    the org skill library (grill-with-docs, tdd, ...)
demo/requirements.md       the vague demo requirement
```
