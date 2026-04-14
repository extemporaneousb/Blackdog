# Blackdog

Blackdog is a machine-native planning and runtime kernel for AI-first repo
work.

It owns a typed workset/task store under a shared control root and a WTAM
kept-change workflow on top of that store. Humans primarily author docs,
approvals, and prompts. Agents mutate planning and runtime state through the
CLI and typed runtime operations.

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
- `blackdog workset put`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cleanup`
- `blackdog summary`
- `blackdog next --workset`
- `blackdog snapshot`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`

Everything else from the legacy backlog/board/supervisor/bootstrap era has
been removed from the active repo surface and must be rebuilt explicitly on
top of the vNext model if it returns.

## Packages

- `blackdog_core`: durable profile, planning/runtime contracts, typed
  semantics, and derived read models
- `blackdog`: WTAM orchestration on top of the core contract
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
one landed commit per successful task attempt, records runtime, releases
claims, and cleans up the task worktree by default.
`blackdog task begin` accepts `--prompt-mode raw|tuned` so the same-thread
entrypoint can either record the user prompt directly or run it through the
repo-local prompt tuning flow before starting the attempt.
Use `blackdog task show` to inspect an active or latest same-thread task,
`blackdog task close --status blocked|failed|abandoned` to close an
in-progress attempt without landing code, and `blackdog task cleanup` to
remove a retained task workspace.
Use `blackdog next --workset WORKSET` for human or recovery-oriented task
selection inside one workset; explicit planned-task flows can still go through
`worktree preview` and `worktree start` when they need that control.

Blackdog has no non-WTAM implementation mode.

Blackdog also has a separate repo lifecycle concern set. Analyze/install/update/refresh,
prompt composition, and attempt inspection now ship as explicit product-layer
workflows, not workset/task operations.

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
regenerates the repo-local skill, and prunes known legacy backlog-era
artifacts from the shared control root.

## Docs

- [docs/INDEX.md](docs/INDEX.md)
- [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
