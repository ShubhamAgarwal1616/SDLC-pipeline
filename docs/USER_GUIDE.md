# SDLC pipeline: user guide

This guide is for people *using* the pipeline: product owners, engineers and reviewers. If you're building the pipeline itself, see `HANDOFF.md` and `PLAN.md`.

## What it does

1. **You write a rough requirement.** The pipeline asks you questions until it's clear, then writes a PRD and an **OpenSpec** spec, split into small shippable changes.
2. **You review the spec as a GitHub PR.** Comment, then `/sdlc revise`. **Merging approves it.** Jira gets an Epic and one Story per change, with acceptance criteria.
3. **You pick a story.** The pipeline implements it test-first in an isolated sandbox, verifies it in a clean container, has a second AI review it, and opens a **code PR**.
4. **You review the code PR.** Comment, then `/sdlc fix`. `/sdlc approve` adds the spec archive commit. **Merging** finishes the story and moves it to Jira Done.

No AI-written code ever runs with your credentials or on the internet, and nothing reaches `main` without a person merging it.

## Get started: the pipeline in your repo (GitHub Actions)

The pipeline runs **inside your repo's GitHub Actions**. You don't build anything or run Docker. You need:

- [uv](https://docs.astral.sh/uv/) (to install the CLI),
- a GitHub token for the repo (or the GitHub CLI logged in): fine-grained, read/write on Contents, Pull requests,
  Issues, Workflows, Secrets, Variables and Administration,
- an OpenRouter API key and a Jira API token (the first `sdlc init` asks for them).

**1. Install the CLI (once per machine)**

```bash
uv tool install git+https://github.com/shubhamagarwal1616/sdlc-pipeline@v0.2.0
```

**2. Set up the repo (once per repo)**: from a clone of it, or with `--repo owner/name`:

```bash
sdlc init --jira-project KAN
```

It asks only for what's missing (input is hidden; values can be saved to `~/.sdlc/.env` for your next repo) and:

- stores the keys as the repo's **Actions secrets** (encrypted before they leave your machine), plus a generated
  `SDLC_STATE_KEY`,
- adds `.sdlc/pipeline.yaml`, `openspec/`, `CLAUDE.md`, the **workflow** `.github/workflows/sdlc.yml` and the
  **"Feature request (SDLC)"** issue form, and creates the labels,
- for an **empty repo**, offers a **starter service** (`kotlin-http4k`: Gradle wrapper, Http4k, Kotest, a health
  endpoint and its test). The AI builds offline and never adds libraries, so declare the ones you'll need in
  `build.gradle.kts` early (a normal PR),
- pushes the first commit to an empty repo, or opens a PR on an existing one: **merge it**.

Then **protect the base branch** (Settings → Branches) so only reviewed PRs reach it.

**3. Ask for a feature.** Open an issue with the **Feature request (SDLC)** template (Problem, Users, Goals). That's
all. Everything else happens in the issue and the PRs:

| The pipeline…                                                         | You reply (in the issue or PR)                                       |
| ---------------------------------------------------------------------- | -------------------------------------------------------------------- |
| asks questions in the issue, one at a time, each with a recommendation | `/sdlc answer <text>`, `/sdlc accept` or `/sdlc done`          |
| opens the**spec PR** and links it in the issue                   | comment on files, then`/sdlc revise`; **merge** to approve   |
| creates the Jira Epic and Stories and lists them in the issue          | `/sdlc implement KAN-12` or `/sdlc implement all`                |
| opens a**code PR** per story                                     | comment, then`/sdlc fix`; `/sdlc approve`; **merge**       |
| posts in the issue if something needs you (budget, failure)            | `/sdlc resume [budgets.per_stage_usd.review=6]` or `/sdlc abort` |

`/sdlc status` and `/sdlc help` work in the issue too. Only repo owners, members and collaborators can drive it.

**Optional settings** (repo Settings → Secrets and variables → Actions):

- **Variables** `SDLC_MODEL_DEFAULT`, `SDLC_MODEL_REVIEW`, …: the models per stage ([Choosing models](#choosing-models)).
- **Secret** `SDLC_GITHUB_TOKEN`: a token for the bot. With it, the bot's PRs trigger your CI checks; without it the
  workflow's built-in token is used, and GitHub doesn't run other workflows on its PRs.

**Features across several repos (GitHub Actions mode).** One repo is the feature's **home**: its issues start
features, and its workflow runs everything. The others are **members**.

1. In the home repo: `sdlc init --registry repos.yaml`. The file lists the repos, what each owns and who depends on
   whom (same format as `config/repos.yaml`); it becomes `.sdlc/repos.yaml`. The secret `SDLC_GITHUB_TOKEN` is
   required: a token with Contents, Pull requests and Issues read/write on **every** repo in the registry.
2. In each member repo: `sdlc init --relay-to owner/home-repo`. It adds `.sdlc/`, `openspec/` and a small relay
   workflow, so comments and merges on the pipeline's PRs there wake the home workflow. Its secret
   `SDLC_RELAY_TOKEN` must be allowed to dispatch to the home repo (Contents: read and write there).
3. Open the issue in the home repo and fill in **Repos** (e.g. `notes-api, notes-client`). You get one spec PR per
   repo, linked to each other; **merge all of them** to approve. `/sdlc implement all` then builds the stories in
   dependency order: a consumer's story starts after its provider's are merged, with the provider's specs as its
   contract.

**How it works:** every issue comment, PR review or merge triggers one job that pulls the pipeline's prebuilt images,
does whatever is due, and saves the run state (encrypted) for the next job. Jobs run one at a time; a long step
(implementing a story takes 15–30 minutes) just delays the next reply. A job cut off mid-step asks you to
`/sdlc resume` in the issue.

## Running it from your machine instead (local mode)

Same pipeline, driven from the terminal. Needs Docker; the images are pulled, not built.

```bash
sdlc up                          # pulls the images, starts the LLM gateway (~/.sdlc/.env holds the keys)
sdlc doctor --repo owner/name    # everything should say pass
sdlc watch --launchd             # optional: a background watcher for PR comments and merges
```

## Day to day (local mode)

### Turn a requirement into stories

```bash
sdlc plan requirements.md --repo owner/name
```

- **Questions** arrive one at a time, each with a suggested answer. Then do one of these:
  - `sdlc answer <run>`: answer interactively. Enter accepts the suggestion.
  - Fill in `runs/<run>/answers.md`, then `sdlc answer <run> --file runs/<run>/answers.md`.
  - `sdlc answer <run> --done`: stop. Anything still open becomes an assumption, which is listed in the PRD for reviewers.
- **The spec PR** appears on GitHub. Leave comments on the files, then comment **`/sdlc revise`**. Merge when you're happy, and the Jira stories are created.

### Implement a story

```bash
sdlc implement KEY-123 --repo owner/name
```

- The story moves to *In Progress*, then *In Review* when the code PR opens.
- **On the code PR:**
  - Comment on the code, then **`/sdlc fix`**. You'll get commits and a reply in each comment's thread.
  - When you're happy: **`/sdlc approve`**, wait for the archive commit, then **merge**. The story moves to *Done*.
  - **`/sdlc abort`** stops the run.

### Features across several repos

Register the repos in `config/repos.yaml`, saying what each owns and who depends on whom. Then:

```bash
sdlc plan requirement.md --repos notes-api,notes-client   # one grill and PRD; one spec PR per repo
# merge ALL the spec PRs, and the Epic plus stories (labelled repo-<name>) are created
sdlc implement --feature KAN-20                           # starts stories in dependency order
sdlc feature status KAN-20
```

A consumer's story starts only after its provider's stories are Done. Its sandbox gets the provider's merged specs read-only as the contract. If any story's run needs you, the feature pauses until it's resolved.

## When something needs you

`sdlc status` lists runs. A run that **escalated** says why, for example "budget exhausted", "loop limit reached" or "stale". Your options:

```bash
sdlc resume <run> [--from <stage>] [--set budgets.per_stage_usd.review=4] [--budget 10]
sdlc abort <run>
sdlc inspect <run> [--stage b4_verify]     # what happened, with artifacts and logs
sdlc report <run>                          # tokens and $ per stage
```

## PR commands

| Comment                 | On      | What happens                                                            |
| ----------------------- | ------- | ----------------------------------------------------------------------- |
| `/sdlc revise [note]` | spec PR | Every comment since the last revision goes back to the spec writer      |
| `/sdlc fix [note]`    | code PR | Every comment since the last push goes back to the implementer          |
| `/sdlc approve`       | code PR | The OpenSpec archive commit is added (living specs updated); then merge |
| `/sdlc abort`         | either  | The run stops                                                           |
| *merge*               | spec PR | Approval: Jira stories are created                                      |
| *merge*               | code PR | The story moves to Done                                                 |

Commands work in any comment: on the PR, on a line, or in a review summary. Only owners, members and collaborators can use them.

## Stacks: Kotlin/Gradle and Node/TypeScript

The pipeline detects the stack from the repo (`gradlew` + `build.gradle.kts`, or `package.json` +
`package-lock.json`), or you pin it with `project.stack: gradle | node` in `.sdlc/pipeline.yaml`. For each, it
fills an **offline dependency cache** from the base branch, builds and tests with no network, counts tests from
JUnit XML, and blocks PRs that add dependencies.

- **Node/TypeScript:** commit `package-lock.json` (it's what gets cached). Default verify commands: `npm ci`,
  `npm run lint --if-present`, `npm test`. Write JUnit XML under `test-results/` to get test counts in the PR
  (the starter does). Install scripts never run in the sandbox.
- **Starters for empty repos:** `sdlc init --skeleton kotlin-http4k` or `--skeleton node-typescript` (no runtime
  dependencies; TypeScript strict, `node:test`).

## Repo-specific rules for the AI

Put a file in `.sdlc/instructions/` named after a stage (`grill.md`, `propose.md`, `implement.md`, `review.md`,
`fix.md`) and it's appended to that stage's instructions, for example "persist through the `OrderRepository` port
only". Keep each under 8 KB. `sdlc init` adds a README there. Runs started after the merge pick it up.

## Changing a requirement mid-way

Before the stories are published you can change the requirement: **edit the issue** (GitHub Actions mode) or run
`sdlc plan new-requirements.md --run <run> --update` (local mode). The pipeline compares the versions, asks only
about what changed, and revises the PRD and the spec PR. Once the stories exist, open a new issue instead.

## Tuning the questions and the review

In `.sdlc/pipeline.yaml`:

```yaml
grill:
  mode: bounded               # or until_clear (default: ask until the checklist is clear)
  max_rounds: 3               # bounded: after this round, open points become assumptions
  questions_per_round: 3      # either mode: batch up to N questions instead of one at a time
  readiness_threshold: 0.8    # bounded: stop once 80% of the checklist is settled
review:
  max_blocking_per_round: 5   # more blocker/major findings wait for the next round
  followup_mode: true         # rounds 2+ check the earlier findings and regressions only
```

A question that repeats one already answered never reaches you (`grill.dedupe_similarity`). Follow-up reviews run
on `llm.stages.review.followup_model` (Sonnet by default; `SDLC_MODEL_REVIEW_FOLLOWUP`), which keeps the cost of
review rounds down.

## Confluence

To mirror specs for readers who don't use GitHub, set in `.sdlc/pipeline.yaml` (or `sdlc init --confluence-space KEY`):

```yaml
project: { docs: confluence }
confluence: { space: ENG, parent_page_id: "123456" }   # fake: true writes the pages to runs/_confluence/ instead
```

When the spec is merged you get a page per requirement (PRD and a stories table) and a child page per change
(scenarios, design, tasks). Each change page's status line follows the story: To do, In review (with the PR), Done.
Credentials: `CONFLUENCE_BASE_URL` (default: your Jira URL + `/wiki`) and `CONFLUENCE_API_TOKEN` (default:
`JIRA_API_TOKEN`). If Confluence fails, Jira is still published and the error is noted.

## Good requirements make good runs

- Use the headings **Problem**, **Users** and **Goals**. Intake flags any that are missing.
- Say *who* is affected and *what success looks like*. The grill fills in the rest.
- Never paste secrets. Intake blocks the run if it finds one.

## Costs (from real runs, via OpenRouter)

| Step                                                            | Typical cost |
| --------------------------------------------------------------- | ------------ |
| Requirement → spec PR (3–4 questions)                         | $0.40–0.75  |
| Story → merged PR (Sonnet implements, Opus reviews 4–7 times) | ~$3          |

Budgets stop a stage before it overspends. Raise them with `resume --set` when it's worth it.

## Choosing models

Each LLM stage's model can be chosen; blank means the org default in `config/org.yaml`. In GitHub Actions mode set these as repo **Variables** (same names); in local mode put them in `~/.sdlc/.env`:

```bash
SDLC_MODEL_DEFAULT=claude-haiku-5-5   # every stage, e.g. for a cheap trial run
SDLC_MODEL_REVIEW=claude-opus-5-5     # one stage: GRILL, PROPOSE, IMPLEMENT, REVIEW, RESOLVE_COMMENTS, REVIEW_FOLLOWUP
SDLC_TEST_MODEL=claude-haiku-5-5      # `sdlc eval`
```

- A repo's `.sdlc/pipeline.yaml` (`llm.stages.<stage>.model`) and `--model stage=model` on the command line override `.env`.
- Models you name in `.env` are added to the allow-list. `SDLC_ALLOWED_MODELS` replaces the whole list.
- The gateway turns `claude-x-5-5` into OpenRouter's `anthropic/claude-x-5.5`. Ids with a slash pass through. Add other mappings with `SDLC_MODEL_MAP_JSON` (local mode: then `sdlc up --recreate`).
- A run keeps the models it started with. `sdlc doctor` and `sdlc config --explain llm` show what new runs will use, and where each value came from.
- Keep the review model different from the implement model: a second model catches more.
