# Blackdog

Blackdog is a machine-native task and attempt runtime for AI-first repo work.

It owns a WTAM kept-change workflow plus typed runtime history under a shared
control root. Humans primarily author docs, approvals, and prompts. Agents use
the CLI to execute isolated tasks, recover interrupted attempts, and record the
results needed for prompt shaping and performance measurement.

## Shipped Surface

The current shipped CLI is deliberately narrow:

- `blackdog init`
- `blackdog repo analyze`
- `blackdog repo install`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task recover`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cancel`
- `blackdog task reopen`
- `blackdog task cleanup`
- `blackdog summary`
- `blackdog snapshot`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`

The shipped surface is split across repo lifecycle/operator-read surfaces
(`repo`, `prompt`, and `attempts`), same-thread `task` execution, status and
history reads, and explicit `worktree` control.

Everything else from the legacy backlog/board/bootstrap/orchestration era remains
removed from the active repo surface and must be rebuilt explicitly on top of
the vNext model if it returns.

## Packages

- `blackdog_core`: durable profile, planning/runtime contracts, typed
  semantics, and derived read models
- `blackdog`: product-layer WTAM orchestration and repo lifecycle workflows on
  top of the core contract
- `blackdog_cli`: thin parser/help/dispatch layer

## Repo Use

In this repo, use `./.VE/bin/blackdog` when the worktree has a local `.VE`.
Do not keep implementation edits in the primary worktree. Run
`./.VE/bin/blackdog worktree preflight` first; if it reports the primary
worktree, move into a branch-backed task worktree before editing files.
For the normal same-thread agent path, prefer `./.VE/bin/blackdog task begin`
to create, claim, and start one task envelope in a single step.
Use `./.VE/bin/blackdog worktree preview` when you want to inspect the WTAM
start plan, prompt receipt, repo contract inputs, and handler actions before a
claim/start.
`blackdog.toml` owns explicit `[[handlers]]` blocks for repo-root and
worktree-local env/runtime setup.
`blackdog worktree start` executes that handler plan, creating the worktree
`.VE`, wiring the repo-root package overlay, linking root-bin fallbacks, and
writing the worktree-local launcher when needed.
`blackdog worktree land` is the canonical success closure surface: it creates
one landed commit per successful task attempt, records runtime and commit
lineage, releases claims, and cleans up the task worktree by default. That
canonical landed commit carries `Blackdog-Workset`, `Blackdog-Task`,
`Blackdog-Attempt`, `Blackdog-Actor`, and `Blackdog-Status` trailers, plus one
`Blackdog-Changed-Path` trailer per changed path and any validation/residual/
follow-up trailers supplied at land time. Runtime `commit` is the task-branch
head Blackdog landed from; `landed_commit` is the canonical commit created on
the target branch.
If an operational blocker prevents landing, such as a dirty primary checkout
or stale branch base, the active attempt remains open so the agent can fix the
blocker and rerun `task land` or `worktree land`.
If the land failure is terminal and safely classifiable, such as a task branch
with no changes relative to the target branch, Blackdog closes the attempt
internally through the same finalizer as `task close` / `worktree close`.
`blackdog task begin` accepts `--prompt-mode raw|tuned|skill` so the
same-thread entrypoint can record a direct user prompt, run the prompt through
repo-local tuning, or record a skill-composed execution prompt with separate
user-prompt lineage.
Use `blackdog task show` to inspect an active or latest same-thread task,
`blackdog task close --status blocked|failed|abandoned` to close an
in-progress attempt without landing code, `blackdog task cancel` and
`blackdog task reopen` to hide or reactivate planned work, and
`blackdog task cleanup` to remove a retained task workspace. Abandoned closes
cancel the task by default, so normal `summary` and `next` stay focused on live
work; use `summary --include-canceled` for audit views.
Direct planned-task authoring is disabled by default. If old planned state
needs migration or repair, `BLACKDOG_ENABLE_WORKSET_COMMANDS=1 blackdog
workset put ...` keeps the low-level escape hatch available without making it
part of normal repo work.

Blackdog has no non-WTAM implementation mode.

Blackdog also has a separate repo lifecycle concern set.
Analyze/install/update/refresh, prompt composition, and attempt inspection now
ship as explicit product-layer workflows and operator read surfaces, not
workset/task operations.

For non-Blackdog repos, `blackdog repo analyze` is the read-only conversion
entrypoint: it inventories agent docs, skills, `.VE`, launcher/profile state,
and ambiguity sources, then emits a proposed conversion plan before anything is
installed. `blackdog repo install` defaults to a managed Blackdog source
checkout under the control root, sourced from GitHub. Use
`--source-root /path/to/blackdog` to override that with a local checkout. When
the target repo is Blackdog itself, install/update reuse that repo as the
source checkout. The shipped Python handler keeps repo-root `.VE` as the
canonical base env and gives each task worktree its own overlay `.VE`.
When install has to write a fresh profile, it seeds `doc_routing_defaults`
from `AGENTS.md` plus common repo docs that already exist, instead of assuming
Blackdog-specific docs are present in the host repo. `repo install` also
ensures `AGENTS.md` carries a managed Blackdog contract block so converted
repos start with explicit WTAM rules in repo docs, not only in the generated
skill. `blackdog repo refresh` rewrites that managed `AGENTS.md` block,
regenerates the repo-local skill plus Codex `agents/openai.yaml` metadata, and
prunes known legacy backlog-era, removed-orchestration, and stale generated
skill artifacts.

## Docs

- [docs/INDEX.md](docs/INDEX.md)
- [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
