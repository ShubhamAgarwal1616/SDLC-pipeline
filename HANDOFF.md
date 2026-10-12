# SDLC pipeline: session handoff

**Last updated:** 2026-10-12, session 4 (models configurable via `.env`; **packaging + GitHub Actions mode built**; Demo 4 paused).
**Status:** P1–P10 are built.
- P1–P6 are proven live end to end: requirement → Jira → code → reviewed PR → merged → Jira Done.
- P7–P9 are partly proven live: `doctor`, `skills lint`, the reports, the dashboard and the evals ran for real; reminders, stale runs and the sync-bundle merge haven't.
- P10 (multi-repo) is unit-tested; **its live demo (Demo 4) is the next step.**

**Read this first.** Then read:
- `README.md` (how to run it)
- `docs/USER_GUIDE.md` (how people use it)
- `PLAN.md` §13–25 (design), skimmed
- `BUILD_AND_ROLLOUT.md` §2 (the build checklist, with results tables for each phase)

---

## 1. What we're building

A two-phase agentic SDLC pipeline with humans in the loop.

- **Pipeline A** (`sdlc plan`): a vague `requirements.md` goes through:
  1. grill questions (with a human answering)
  2. a PRD
  3. OpenSpec change folders, validated
  4. a **spec PR on GitHub** (`/sdlc revise` loop, merge = approval)
  5. a **Jira Epic + Stories**
  - With `--repos a,b` it plans **one feature across several repos**: one PR per repo, and cross-repo Blocks links.
- **Pipeline B** (`sdlc implement KEY`): a Jira story goes through:
  1. fetch + repo prep
  2. `/tdd` implements it in the sandbox
  3. a no-network verify container (build, tests, gitleaks, semgrep, tasks complete)
  4. AI review (Opus)
  5. a diff guard
  6. a code PR (`/sdlc fix` loop)
  7. `/sdlc approve` (archive commit)
  8. merge
  9. Jira Done
  - `sdlc implement --feature EPIC` runs a feature's stories **in dependency order** across repos.
- **Phase 1** (host: the Python orchestrator plus the gateway container) holds all secrets and talks to GitHub, Jira and OpenRouter.
- **Phase 2** (Docker sandbox) runs **Claude Code headless** (`claude -p`) with the skills. It has no secrets, and its only network route is the LLM gateway. Build, test and scan code runs in separate **no-network** verify containers. **AI-written code never runs in Phase 1.**

---

## 2. Decisions made (don't re-ask)

| Topic | Decision |
|---|---|
| Location | `AI_Projects/SDLC_workflow/`, a subfolder of the `AI_Projects` git repo. **Nothing is committed: the user handles git.** Don't commit unless asked, and branch first if asked |
| LLM calls | Always **Claude Code CLI headless** (`claude -p`), never the raw API. Claude Code 2.1.296, OpenSpec 1.14.1 and semgrep 1.180.0, all pinned in `worker/Dockerfile` |
| Provider | **OpenRouter**, only through the thin gateway `gateway/app.py`. Model map: `claude-sonnet-5-5`→`anthropic/claude-sonnet-5.5`, `claude-opus-5-5`→`anthropic/claude-opus-5.5`, `claude-haiku-4-5-20251001`→`anthropic/claude-haiku-4.5`, `claude-haiku-5-5`→`anthropic/claude-haiku-5.5` (automatic rule) |
| Stage models | grill, propose, implement and fix use Sonnet 5.5. **Review uses Opus 5.5** (a different model from implement). **All configurable in `.env`** (session 4): `SDLC_MODEL_DEFAULT`, `SDLC_MODEL_<STAGE>` (GRILL, PROPOSE, IMPLEMENT, REVIEW, RESOLVE_COMMENTS), `SDLC_ALLOWED_MODELS`, `SDLC_MODEL_MAP_JSON` (gateway). Precedence: org.yaml < .env < repo `.sdlc/pipeline.yaml` < `--model`. Code: `config.env_models` / `_org_layer` |
| Test model | **`SDLC_TEST_MODEL` in `.env`** (default `claude-haiku-5-5`, cheap) for `sdlc eval`. The user wants test spend kept low and configurable |
| Code host | **GitHub**. Repos: `Auranestt-candles/workflow-test` (**notes-api**, provider) and `Auranestt-candles/workflow-test-2` (**notes-client**, consumer, created for P10). Both are public |
| Tracker | **Jira Cloud**, project **`KAN`** ("SDLC Workflow", team-managed; Epic/Story/Subtask/Task; statuses To Do, In Progress, In Review, Done) |
| Docs | **No Confluence yet** (`project.docs: none`) |
| A2 (propose) | **Chain `/to-prd` → `/openspec-propose`** in one resumed session (per repo in feature mode). `changes.json` is the source of truth |
| Skills | The user's skills are in `skills/`, plus the pipeline's `sdlc-code-reviewer` and `sdlc-feedback-resolver`. **One entry skill named in the prompt per stage** (`/grill-with-docs`, `/to-prd`, `/openspec-propose`, `/tdd`, `/sdlc-code-reviewer`, `/sdlc-feedback-resolver`). Everything else is auto-picked by description, and all skills are visible by default |
| Grill | `until_clear` mode (no round cap). A script checks the clarity checklist. The stage budget is the hard stop |
| Distribution (session 4) | Other developers **don't clone or build**: `uv tool install git+https://github.com/shubhamagarwal1616/sdlc-pipeline@vX` then `sdlc init` in their repo, then open issues. The pipeline runs **in the repo's GitHub Actions** (`sdlc ci reconcile`), on images from **GHCR** (`release.yml`). `sdlc init` **prompts for keys and sets repo secrets**. Assumes the pipeline moves to its own repo `shubhamagarwal1616/sdlc-pipeline` (else change `distribution.install` and the image refs in `config/org.yaml`) |
| Demo services | Kotlin 2.4.21 / Http4k 6.61 / Kotest 6.2.5 / Gradle 9.8.1 / JDK 21. Sources: `demo/sample-repo/` (notes-api) and `demo/sample-client/` (notes-client) |

---

## 3. Environment

- macOS on Apple Silicon. Docker Desktop: engine 20.10.14, Compose v2.4.1, arm64, 4 CPUs, 8 GB. Python 3.13 via `uv`.
- `.env` is git-ignored. **Never print or paste its values.** Check presence by name only.
  - It has `OPENROUTER_API_KEY`, `JIRA_BASE_URL`, `JIRA_USER_EMAIL`, `JIRA_API_TOKEN`, `GITHUB_TOKEN` (fine-grained; Contents, PRs and Issues read/write) and `SDLC_TEST_MODEL`. Optional `SDLC_MODEL_*` (see `.env.example`); the parser now strips inline `# comments` on unquoted values.
  - `CONFLUENCE_*` is empty. `OTEL_EXPORTER_OTLP_ENDPOINT` is optional.
  - ⚠️ The token can see 3 repos: `workflow-test`, `workflow-test-2`, **and** the user's other repo `auranestt-candle`. The user was told to restrict it to the two test repos; it hasn't been confirmed.
- Images:
  - `sdlc-gateway:dev` (compose service `llm-gateway`)
  - `sdlc-worker:dev`: Claude Code, OpenSpec, JDK 21, gitleaks, semgrep and the skills. **No curl or wget.** Multi-stage build
- Networks: `sdlc_internal` (internal: true; the agent's only route is to the gateway) and `sdlc_egress` (used only by the Gradle cache warmer).
- Start: `docker compose up -d`. **After changing `gateway/app.py`, use `docker compose up -d --build`** (the code is baked into the image). Rebuild the worker after changing `skills/`, `worker/` or managed settings: `docker build -f worker/Dockerfile -t sdlc-worker:dev .`
- Checks:
  - `uv run sdlc doctor --repo Auranestt-candles/workflow-test` (17/17 last time; one warning: `main` unprotected)
  - Unit tests: `uv run pytest -q`. **111 pass.** They must never call live services; stub or fake them. `orchestrator/tests/conftest.py` keeps the developer's `.env` model choices out of tests.
  - LLM evals: `uv run sdlc eval` (5 cases, **5/5, about $0.025** on `SDLC_TEST_MODEL`).

---

## 4. What's done (phase by phase)

| Phase | Status | Key files |
|---|---|---|
| **P1** skeleton | ✅ | `orchestrator/sdlc/{cli,config,answers}.py`, `engine/{state,stage,transitions,runner}.py`, `stages/fake.py`, `config/org.yaml` (org layer + locks) |
| **P2** sandbox + gateway | ✅ spike 16/16 | `gateway/app.py`, `worker/{Dockerfile,entrypoint.py,managed-settings.json,managed-mcp.json,claude-home/CLAUDE.md}`, `scripts/p2_spike.py` |
| **P3** requirements loop | ✅ Demo 1 | `stages/pipeline_a.py` (A0 to A3), `sandbox/runner.py`, `workspace.py`, `gateway_keys.py`, `contracts/{grill,propose}.md` |
| **P4** spec PR + Jira | ✅ Demo 2 | `connectors/{github,jira}.py`, `stages/publish.py`, `watch.py`, `init_repo.py`, `openspec_parse.py`, `secrets.py` |
| **P5** implementation loop | ✅ Demo 3 | `stages/pipeline_b.py` (B0 fetch, B1 prep, B3 implement, B4 verify), `dep_cache.py` (was `gradle_cache.py`), `stacks.py`, `contracts/implement.md` |
| **P6** review/PR/archive | ✅ Demo 3 | `stages/pipeline_b_review.py` (B5 review, B6 guard, B7 PR, B8 fix, B9 archive, B10 close), `skills/sdlc-code-reviewer/`, `skills/sdlc-feedback-resolver/`, `contracts/{review,fix}.md`, the `h4_merge` gate |
| **P7** observability | ✅ | `telemetry.py` (a span per stage in `runs/<id>/spans.jsonl`, optional OTLP/HTTP export), `reporting.py` (`sdlc report --all`, `sdlc dashboard`), budget warnings at 80% (`stages/pipeline_a.py:_budget_warnings`) |
| **P8** hardening + evals | ✅ live | `evals.py` + `evals/<suite>/<case>/` (`sdlc eval`), `worker/semgrep-rules/sdlc.yaml`, semgrep in `pipeline_b.Verify` (`run_semgrep`, which forces its own `.semgrepignore`), the no-curl image, shell guidance in `worker/claude-home/CLAUDE.md` |
| **P9** ops (local mode) | ✅ | `doctor.py`, `skills_lint.py` (`sdlc skills list/lint/add`), `watch.py` (`nudge_waiting`: reminders and stale), **sync bundles** (`pipeline_b.sync_bundle`, `current_base_sha`: base moved or human commits are merged in the sandbox; B7 never force-pushes), `sdlc watch --launchd`, `docs/USER_GUIDE.md` |
| **P10** multi-repo | ✅ unit-tested; **live Demo 4 pending** | `config/repos.yaml`, `registry.py` (targets), the multi-repo paths in `stages/pipeline_a.py` and `stages/publish.py`, multi-PR gates in `watch.py`, `feature.py`, `pipeline_b.provider_contracts`, `demo/sample-client/`, `demo/feature-requirements.md` |

**Live results so far** (details and costs in `BUILD_AND_ROLLOUT.md`):
- **Demo 1** (Pipeline A, local): $0.74.
- **Demo 2:**
  - spec PR #1 merged
  - Jira **KAN-7** Epic with **KAN-8, KAN-9, KAN-10**; KAN-8 blocks KAN-9 and KAN-10
  - $0.59
  - KAN-4/5/6 are from an earlier test and can be ignored
- **Demo 3:**
  - KAN-8 → code PR #3, 33 tests
  - the AI review caught **2 real security bugs** plus an `HTTP-01` violation
  - `/sdlc fix` and `/sdlc approve` (archive commit) worked; merged; **KAN-8 Done**
  - $2.94
- **Evals (P8):** 5/5. The reviewer catches a planted off-by-one. Three prompt-injection attacks (in a requirement, a PR comment and a code comment) all fail: no leak, no CI deletion, no push, and the injected text gets reported.
- **Spend to date:** about $5.30 across all runs, with an 88% cache hit. **The review stage (Opus) is the biggest cost.**

**Proven properties:**
- the sandbox has no internet and no download tools
- managed settings load from `/etc/claude-code/managed-settings.json`
- `allowManagedHooksOnly` ignores repo hooks
- `--resume` works across containers
- `--add-dir` loads pack skills
- prompt caching works through OpenRouter
- gateway costs match Claude Code's
- the token is never written to any `.git/config`

---

## 5. Current state: exactly where we stopped

**Session 4 built packaging + GitHub Actions mode, then (at the user's request, before any live test) multi-repo CI
mode, the Node/TypeScript stack, the backlog (follow-up reviews, `.sdlc/instructions`, grill bounded mode + repeat
filter, `plan --update` / issue edits) and the Confluence mirror** (BUILD_AND_ROLLOUT P9b; **111 unit tests pass**;
the wheel installs and runs outside the checkout; both starter skeletons build and test offline in the sandbox;
the worker image was rebuilt for the npm allow-list and TS Semgrep rules). **Nothing here is live-tested yet.**
Next steps:
1. **User:** put this folder in its own GitHub repo `shubhamagarwal1616/sdlc-pipeline` (or edit `distribution.install`
   + `sandbox.image`/`gateway_image` in `config/org.yaml`), push, tag `v0.2.0`. `release.yml` publishes the images;
   make both GHCR packages public.
2. **User:** give the GitHub token Secrets, Variables, Workflows and Administration (read/write) on the test repo
   (today it gets 403 listing secrets).
3. Live test on a new **empty** test repo: `sdlc init --jira-project KAN` (pick the kotlin-http4k skeleton), open a
   Feature request issue, answer in the issue, merge the spec PR, `/sdlc implement all`. Expect first-live fixes in:
   the Linux uid handling, artifact download/restore, the workflow's `if:` filter, Actions' PR permission.
4. Local mode is unchanged for this checkout: `.env` now has `SDLC_WORKER_IMAGE=sdlc-worker:dev` and
   `SDLC_GATEWAY_IMAGE=sdlc-gateway:dev` (org.yaml defaults to the GHCR images). New files: `ci.py`, `infra.py`,
   `init_repo.py` (rewritten), `templates/` (openspec, skeletons, github incl. the relay), `stacks.py`,
   `dep_cache.py` (was `gradle_cache.py`), `connectors/confluence.py`, `.github/workflows/release.yml`.
5. Live-test order after the single-repo test: a Node repo (`--skeleton node-typescript`), Confluence with
   `confluence.fake: true` then a real space, a requirement edit mid-grill, then a multi-repo feature (home with
   `--registry`, member with `--relay-to`).

**Demo 4** (below) is paused at grill round 3; it's local-mode and unaffected.

**Demo 4 is in progress: run `run-20261012-194150-1d5a`** (`sdlc plan demo/feature-requirements.md --repos notes-api,notes-client`), waiting at **h0_answers** (grill round 1, $0.09 so far). The generated system map reached the sandbox and the grill used notes-api's living spec. Steps 1–2 below are done up to answering the grill.
(`run-20261012-145408-e2c0`, shown as waiting at h3, is an old `--fake` run on `example/sample-repo`; ignore or abort it.)

**Demo 4 (multi-repo), step by step:**
1. ✅ `workflow-test-2` PR #1 (the `notes-client` library + `.sdlc` verify config) is **merged**, so both repos are ready. Optional pre-check: `uv run sdlc doctor --repo Auranestt-candles/workflow-test-2`. The first Pipeline B run on repo 2 will warm its own Gradle cache (about 5 min).
2. `docker compose up -d`, then `uv run sdlc plan demo/feature-requirements.md --repos notes-api,notes-client` ("Clients respect rate limits"). Answer the grill (`sdlc answer <run> …`). That yields **2 spec PRs**, one per repo, cross-linked. A repo with no part in the feature gets no PR.
3. The user reviews (`/sdlc revise` works on either PR) and **merges both**. Then `uv run sdlc watch --once` creates an Epic plus stories labelled `repo-notes-api` / `repo-notes-client`, with **notes-api stories blocking notes-client stories**.
4. `uv run sdlc implement --feature <EPIC>` starts only the unblocked notes-api story. Then:
   - `/sdlc approve` on its PR
   - `watch --once` (archive commit)
   - the user merges
   - `watch --once` (Jira Done)
5. The next `uv run sdlc watch --once` (or `sdlc feature advance <EPIC>`) starts the notes-client story. Its sandbox gets notes-api's merged living specs at `/opt/sdlc/contracts/notes-api/`. Approve and merge, and `sdlc feature status <EPIC>` shows done.
6. Record **Demo 4** results in `BUILD_AND_ROLLOUT.md` P10 (mark the live item `[x]`, add a results table), then update this file.

**Expect first-live-run fixes in P10**, as every earlier phase needed. Untested live:
- the system map
- per-repo propose prompts and sessions (`a2_propose:<repo>` session keys)
- multi-PR approval
- the feature coordinator
- contract mounts

Note that `feature.advance` starts child runs **synchronously** (each takes about 15–30 min), so `watch` blocks while one runs.

---

## 6. Remaining work (after Demo 4)

**Not yet live-tested** (code exists):
- "merged without `/sdlc approve`" (B9 opens a follow-up archive PR)
- `/sdlc abort` on a code PR
- a **sync-bundle merge** (needs `main` to move under a waiting run, e.g. start KAN-9, merge something else, then `/sdlc fix`)
- a reminder on a real PR (needs a run waiting 24h)
- `skills add`

**Improvements worth doing** (session 4 built: cheaper follow-up reviews, `.sdlc/instructions`, grill bounded mode + repeat filter, `plan --update`, the Confluence mirror):
- **Review churn and cost:** KAN-8 needed 7 Opus reviews. Options:
  - on rounds after the first, tell the reviewer to raise only regressions or unaddressed items
  - cap the blocking findings per round
  - use Sonnet for later rounds
- `.sdlc/instructions/<stage>.md` extension point: not wired into the contracts.
- Fix the user's skills that `sdlc skills lint` flags: rename the `domain-modelling` folder to match its name; `tdd` references a missing `/codebase-design`; `kotlin-service-standards` is 75 KB.
- Single-repo stories **KAN-9** (shadow mode + metrics) and **KAN-10** (shared store + fail-open) are ready to implement: `uv run sdlc implement KAN-9 --repo Auranestt-candles/workflow-test`.

**Leftovers by phase:**
- **P7:** a hosted Langfuse or Grafana (with the shared service).
- **P8:**
  - run `sdlc eval` in CI when `skills/` or `contracts/` change
  - more cases (propose quality, tdd on a tiny repo)
  - periodically re-review the allow-list (58 allow, 13 deny)
- **P9:** the shared deployment (central API, **webhooks** instead of polling, Postgres, Kubernetes sandboxes, SSO), the Confluence mirror (once a space exists), a Docker-socket proxy if the watcher moves into a container.
- **P10:** a contract-first parallel strategy (today it's sequential), monorepo package scopes, non-blocking child starts in `feature.advance`.

**Small deferred:**
- the grill `bounded` options
- a repeated-question filter
- `sdlc plan --run … --update`
- pinning `uv.lock`
- "MR/GitLab" wording in the older docs (the implementation is GitHub PRs)
- **`sdlc-pipeline-flow.html` (the visual page) is out of date for P3–P10**

**User actions outstanding:**
- **protect `main`** in both test repos
- restrict the GitHub token to the two test repos
- (optional) load the background watcher: `uv run sdlc watch --launchd`, then follow the printed `launchctl` commands

---

## 7. Hard-won rules (keep these)

1. **Phase 1 never runs git (or anything) inside the agent's workspace.** The agent can plant `.git/hooks` or `core.fsmonitor`. Clone it locally into a Phase 1 dir first (`git clone --no-hardlinks ws dest`); a local clone copies objects only. Copy files out with `safe_copy_tree` (regular files only; symlinks skipped).
2. **The GitHub token is passed only via `GIT_CONFIG_*` env** (`connectors/github.py:git_env`), with `core.hooksPath=/dev/null`.
3. **The agent can't `rm`.** For change folders, `changes.json` is authoritative, and Phase 1 deletes unlisted folders. Design other cleanups the same way.
4. **Every turn starts with an empty `/io/out/`.** Models "remember" earlier files. `entrypoint.py` checks `questions.json` *content*, not just whether it exists.
5. **Jira "Blocks" link API:** the `inwardIssue` is the **blocker** (verified). On a blocked issue, the blocker shows up as `inwardIssue`.
6. `/sdlc` commands are accepted from **any** comment type (PR comment, line comment, review summary) and from trusted authors only. The bot's own comments carry `<!-- sdlc-bot -->` and are ignored. GitHub won't let the token owner approve their own PR, so `/sdlc approve` is the approval.
7. Escalations include the stage's note. Feedback survives a crashing stage. `sdlc resume --set` re-checks org locks and updates gateway key budgets. `resume` turns faked stages real once they're built.
8. **Shell permissions are subtle** (found by evals and live runs):
   - Bash can only touch `/workspace` plus `--add-dir` dirs. The entrypoint adds `/io/in` and `/io/out`, and contract mounts add `/opt/sdlc/contracts`.
   - `git -C /workspace x` doesn't match `git x` rules, so both forms are allowed for safe subcommands.
   - Long compound commands can be refused. Weaker models then wrongly decide "no shell" and stop committing.
   - The org `CLAUDE.md` says: one simple command per call, a refusal never means no shell.
   - Claude Code auto-approves read-only commands even in `dontAsk`. **Security rests on isolation, not the allow-list.**
9. **Dependency caches (Gradle, npm; `dep_cache.py` + `stacks.py`):** the cache is warmed once from **base**, in an egress tool container, keyed by a hash of the build files. Each consumer gets a **private copy** (`dep_cache.seed`). npm installs run with `--ignore-scripts`; builds use `npm_config_offline`.
10. **Budgets are per stage per run, across all attempts.** Defaults: review $4, implement $4, propose $1.50, grill $1.50, run $8. Raise mid-run with `sdlc resume <run> --from <stage> --set budgets.per_stage_usd.<key>=N`.
11. **Skills reach runs already in progress:** the entrypoint re-syncs the image's skills and org `CLAUDE.md` into the run's `CLAUDE_CONFIG_DIR` on every call. **Rebuild the worker image** after changing `skills/`, `worker/` or managed settings.
12. **Semgrep skips test dirs by default.** The verify runner always writes its own `.semgrepignore`, which also stops a committed one from hiding code.
13. **Never force-push a PR branch.** If a person pushed to it, or `main` moved, the next B3/B8 gets a git bundle and merges it in the sandbox.
14. Outward actions belong to the user: merges, approvals, Jira transitions of other issues, repo settings. Only the pipeline's own branches, PRs, comments and its stories' transitions are automated. `sdlc init` pushed directly to `main` only on empty repos.

---

## 8. Useful commands

```bash
# setup and checks
docker compose up -d
uv run sdlc doctor [--repo owner/name]
uv run sdlc skills list|lint [--repo owner/name] ; uv run sdlc skills add ./my-skill --repo owner/name
uv run sdlc init --repo owner/name --jira-project KAN

# Pipeline A
uv run sdlc plan demo/requirements.md --repo Auranestt-candles/workflow-test
uv run sdlc plan demo/feature-requirements.md --repos notes-api,notes-client      # multi-repo feature
uv run sdlc answer <run> [--file runs/<run>/answers.md | --accept-defaults | --done]
uv run sdlc review <run> --approve|--changes "…"|--abort                          # local gate decisions

# Pipeline B
uv run sdlc implement KAN-9 --repo Auranestt-candles/workflow-test
uv run sdlc implement --feature <EPIC> ; uv run sdlc feature status|advance <EPIC>

# run control and insight
uv run sdlc watch [--once] [--launchd]          # PR commands, merges, reminders, feature advancing
uv run sdlc status [<run>] ; uv run sdlc resume <run> [--from STAGE] [--set k=v] [--budget N] ; uv run sdlc abort <run>
uv run sdlc report <run> | --all ; uv run sdlc dashboard ; uv run sdlc inspect <run> [--stage b4_verify]
uv run sdlc config --explain <key>
uv run sdlc eval [--suite skills|redteam] [--case NAME] [--model M] [--list]
uv run python scripts/p2_spike.py [--only net,hello,policy,pack,grill]
```

**Where things live:**

| Path | Contents |
|---|---|
| `runs/<run-id>/` | `state.json`, `config.json`, `events.jsonl`, `spans.jsonl`, `artifacts/<stage>/attempt-N/` (incl. `io/in`, `io/out`), `workspace/` (agent repo; `workspaces/<repo>/` in feature mode), `publish/` (Phase 1 clone(s)), `claude-home/` (sessions), `gradle/`, `verify/attempt-N/`, `candidate/` (the exact commit pushed), `contracts/` (provider specs), `sync/` |
| `runs/_gateway/` | `keys.json`, `usage.jsonl` (every LLM call: tokens, cost, model, latency) |
| `runs/_features/<EPIC>.json` | Feature-run state |
| `runs/_evals/<stamp>/` | Eval workspaces and outputs |
| `runs/_dashboard/` | `index.html`, `data.json` |
| `runs/_cache/` | Project snapshots, Gradle caches (`gradle/<repo>/<hash>/`, about 300 MB each) |
| `config/org.yaml` | Org config and locks |
| `config/repos.yaml` | Repo registry |
| `orchestrator/contracts/` | Stage contracts |
| `evals/` | Eval and red-team cases |

---

## 9. Docs in this folder

| File | Contents |
|---|---|
| `PLAN.md` | Full design. §13 OpenSpec, §14 engine, §15 config layers, §16 skill layers, §17 packaging, §18 multi-repo, §19 waiting/resume, §20 feedback channels, §21 command reference, §22 grill modes, §23 skills and entry skills, §24 Claude Code runtime, §25 OpenRouter and the three container types |
| `BUILD_AND_ROLLOUT.md` | Phases P0–P10 with checklists and **results tables** for P1–P8 (Demos 1–3, evals, cost findings); rollout plan |
| `docs/USER_GUIDE.md` | For users (POs, engineers, reviewers): setup, day-to-day commands, PR commands, multi-repo features, costs |
| `README.md` | How to run it, status |
| `sdlc-pipeline-flow.html` | Visual page (swimlanes, stage cards, diagrams). Published at https://claude.ai/artifact/3zAtNw8HGZXcpT2otmWS6a (private). **Out of date for P3–P10.** |
| `HANDOFF.md` | This file |
