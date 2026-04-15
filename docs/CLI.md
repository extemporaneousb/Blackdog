# CLI Reference

The executable name is `blackdog`.

`blackdog_cli` is a thin adapter. It parses arguments, prints help, and
dispatches into `blackdog_core` and `blackdog`. It does not own planning or
runtime logic.

## Shipped Commands

### `blackdog init`

Write a default repo-local `blackdog.toml` profile.

The default profile includes explicit `[[handlers]]` blocks for:

- `python-overlay-venv`
- `blackdog-runtime`

When the target repo already has agent-facing docs, `init` seeds
`[taxonomy].doc_routing_defaults` from `AGENTS.md` plus the common doc names
that already exist in that repo.

```bash
blackdog init --project-root /path/to/repo --project-name "Repo Name"
```

### `blackdog repo analyze`

Inspect a target repo and emit a proposed Blackdog conversion plan without
mutating that repo.

The analysis inventories:

- agent entrypoint docs and package-level `AGENTS.md` files
- repo-local Codex skills under `.codex/skills/`
- repo-local `.VE` and `blackdog` launcher state
- `blackdog.toml` presence, routed docs, and load errors
- likely ambiguity sources where docs or skills bypass the Blackdog contract

```bash
blackdog repo analyze --project-root /path/to/repo
blackdog repo analyze --project-root /path/to/repo --json
```

Important flags:

- `--project-root`

`repo analyze` is the read-only first step for converting an existing repo. It
does not install or refresh anything. Instead it reports findings plus a
proposed sequence of repo-owned and Blackdog-managed changes so the user can
review the conversion plan before `repo install`.

### `blackdog repo install`

Create or repair the minimum repo-local Blackdog contract:

- repo-local `.VE`
- repo-local `blackdog` launcher
- `blackdog.toml` when missing
- explicit handler blocks when the profile still relies on synthesized defaults
- a managed Blackdog contract section in `AGENTS.md`
- repo-local managed skill under `.codex/skills/<repo-slug>/SKILL.md` when missing

```bash
blackdog repo install --project-root /path/to/repo --project-name "Repo Name"
blackdog repo install --project-root /path/to/repo --source-root /path/to/blackdog
```

Important flags:

- `--project-root`
- optional `--project-name`
- optional `--source-root`

`repo install` requires the target path to be inside a git repo. By default it
creates or reuses a managed Blackdog source checkout under the control root,
sourced from GitHub. Use `--source-root` to point the repo-local launcher at a
local Blackdog checkout instead. When the target repo is Blackdog itself,
install uses that repo as the source checkout. The shipped Python handler keeps
repo-root `.VE` as the canonical base env; WTAM worktrees later get their own
overlay `.VE` rooted at the task worktree. If `blackdog.toml` or the repo-local
skill already exist, install preserves repo-owned files and repairs runtime
artifacts through handler actions. When install has to create `blackdog.toml`,
it seeds `doc_routing_defaults` from `AGENTS.md` plus common repo docs that
already exist in the host repo so the initial contract matches the converted
repo instead of Blackdog's own docs.

### `blackdog repo update`

Refresh the repo-local `blackdog` launcher from a Blackdog source checkout.

```bash
blackdog repo update --project-root /path/to/repo
blackdog repo update --project-root /path/to/repo --source-root /path/to/blackdog
```

Important flags:

- `--project-root`
- optional `--source-root`

`repo update` requires an existing `blackdog.toml`. It repairs or replaces the
repo-local launcher and preserves repo-owned contract files such as the skill.
When using the managed source checkout path, it also fast-forwards that source
checkout from GitHub. `repo update` does not silently rewrite custom handler
config, but it does execute the configured handlers and report their actions.

### `blackdog repo refresh`

Regenerate the managed repo-local Blackdog scaffold from `blackdog.toml`.

```bash
blackdog repo refresh --project-root /path/to/repo
```

Important flags:

- `--project-root`

`repo refresh` requires an existing `blackdog.toml`. It rewrites the managed
Blackdog section in `AGENTS.md` and the repo-local managed skill at
`.codex/skills/<repo-slug>/SKILL.md` so both match the current shipped product
surface and routed-doc contract. It also validates the configured handlers,
migrates the legacy
`.codex/skills/blackdog/SKILL.md` path when needed, and prunes known legacy
backlog-era artifacts from the shared control root.

### `blackdog prompt preview`

Preview repo-contract prompt composition without starting task execution.

```bash
blackdog prompt preview \
  --project-root /path/to/repo \
  --prompt "Round out the repo lifecycle MVP."
```

Important flags:

- `--project-root`
- exactly one of `--prompt` or `--prompt-file`
- optional `--show-prompt`
- optional `--expand-skill-text`
- optional `--expand-contract`

`prompt preview` is read-only. It shows:

- prompt hash and source
- repo lifecycle commands Blackdog expects in that repo
- routed contract docs and the repo-local managed skill
- the composed prompt text when `--show-prompt` is set

Use `--expand-skill-text` when you want the repo-local skill text inlined.
Use `--expand-contract` when you want routed doc text inlined as well.

### `blackdog prompt tune`

Rewrite a request into a repo-contract-aware prompt.

```bash
blackdog prompt tune \
  --project-root /path/to/repo \
  --prompt "Round out the repo lifecycle MVP."
```

Important flags:

- `--project-root`
- exactly one of `--prompt` or `--prompt-file`
- optional `--expand-skill-text`
- optional `--expand-contract`

Text output emits the tuned prompt directly. `--json` returns the tuned prompt
plus prompt-hash and contract metadata.

### `blackdog workset put`

Create or update one workset in `planning.json`.
The same payload may also carry optional `task_states` rows, which patch the
matching workset in `runtime.json`.

```bash
blackdog workset put --project-root /path/to/repo --file workset.json
blackdog workset put --project-root /path/to/repo --json '{"id":"kernel", ...}'
```

Payload shape:

- `id`
- `title`
- optional `scope`
- optional `visibility`
- optional `policies`
- optional `workspace`
- optional `branch_intent`
- `tasks`
- optional `task_states`

### `blackdog supervisor start`

Claim one workset for supervisor-managed execution and emit the current
dispatch view.

```bash
blackdog supervisor start \
  --project-root /path/to/repo \
  --workset kernel \
  --actor codex \
  --parallelism 2
```

Important flags:

- `--project-root`
- `--workset`
- `--actor`
- optional `--parallelism`
- optional `--note`

`supervisor start` claims the workset for `workset_manager`. It does not start
worker attempts by itself. Instead it returns the current active tasks, ready
tasks, and a bounded dispatch set for the requested parallelism so a
coordinating agent can launch worker conversations intentionally.

### `blackdog supervisor show`

Inspect the current supervisor view for one workset without mutating runtime
state.

```bash
blackdog supervisor show --project-root /path/to/repo --workset kernel
```

Important flags:

- `--project-root`
- `--workset`
- optional `--parallelism`

`supervisor show` reports:

- the current workset claim
- the current phase such as `dispatch`, `monitor`, `blocked`, or `complete`
- active worker tasks
- ready tasks and dispatch candidates up to the parallelism cap
- recent attempts and recommended next actions

### `blackdog supervisor checkpoint`

Record a supervisor review checkpoint and emit the current dispatch view.

```bash
blackdog supervisor checkpoint \
  --project-root /path/to/repo \
  --workset kernel \
  --actor codex \
  --parallelism 2 \
  --note "reviewed task KERN-1"
```

Important flags:

- `--project-root`
- `--workset`
- `--actor`
- optional `--parallelism`
- optional `--note`

`supervisor checkpoint` requires an active `workset_manager` claim owned by the
given actor. It appends one `supervisor.checkpoint` event and returns the same
dispatch/monitor view as `supervisor show`.

### `blackdog supervisor release`

Release the supervisor claim after review or completion.

```bash
blackdog supervisor release \
  --project-root /path/to/repo \
  --workset kernel \
  --actor codex \
  --summary "kernel workset complete"
```

Important flags:

- `--project-root`
- `--workset`
- `--actor`
- optional `--parallelism`
- optional `--summary`
- optional `--note`

`supervisor release` refuses to release the workset while task claims are still
active. Child worker attempts must finish or close first. Once released, the
command returns the post-release workset view so the coordinating agent can
summarize or replan.

### `blackdog task begin`

Create or reuse one task envelope and start the WTAM attempt.

```bash
blackdog task begin \
  --project-root /path/to/repo \
  --actor codex \
  --prompt "Implement the same-thread slice." \
  --prompt-mode raw
```

Important flags:

- `--project-root`
- `--actor`
- exactly one of `--prompt` or `--prompt-file`
- optional `--prompt-mode raw|tuned`
- optional `--workset`
- optional `--task`
- optional `--title`
- optional `--branch`
- optional `--from`
- optional `--path`
- optional `--model`
- optional `--reasoning-effort`
- optional `--note`
- optional `--show-prompt`

`task begin` is the default same-thread agent entrypoint. When `--workset` and
`--task` are omitted, it creates a one-task workset automatically, claims it
for the caller, records both the raw user prompt receipt and the execution
prompt receipt, provisions the task worktree, and starts the WTAM attempt in
one command.

`--prompt-mode raw` records the supplied prompt directly. `--prompt-mode tuned`
runs the user request through `blackdog prompt tune` first and records the
tuned execution prompt as the attempt prompt receipt. The prompt receipt stores
its `mode` as `raw` or `tuned`. The separate `user_prompt_receipt` keeps the
raw user request for later audit and prompt-tuning review.

### `blackdog task show`

Inspect the current active task, or the latest task if none is active, for the
task worktree you are in.

```bash
blackdog task show --project-root /path/to/repo
blackdog task show --project-root /path/to/repo --workset kernel --task KERN-1
```

Important flags:

- `--project-root`
- optional `--workset`
- optional `--task`

When `--workset` and `--task` are omitted, `task show` infers the task from the
current task worktree. This is the same-thread recovery read surface that
avoids repeating ids on every follow-on command. It reports both the raw user
prompt lineage and the execution-prompt lineage when those differ.

### `blackdog task land`

Land the current task and close it.

```bash
blackdog task land \
  --project-root /path/to/repo \
  --summary "finished the same-thread slice"
```

Important flags:

- `--project-root`
- optional `--workset`
- optional `--task`
- optional `--actor`
- required `--summary`
- repeatable `--validation NAME=STATUS`
- repeatable `--residual`
- repeatable `--followup`
- optional `--note`
- optional `--keep-worktree`

When `--workset` and `--task` are omitted, `task land` infers the active task
from the current task worktree and reuses the active attempt actor. It then
delegates to the canonical `worktree land` success-closure path. Use
`--keep-worktree` when you want to retain the task workspace and close it later
through `task cleanup`.

### `blackdog task close`

Close the current task without landing code.

```bash
blackdog task close \
  --project-root /path/to/repo \
  --status blocked \
  --summary "blocked on fixture mismatch"
```

Important flags:

- `--project-root`
- optional `--workset`
- optional `--task`
- optional `--actor`
- required `--status blocked|failed|abandoned`
- required `--summary`
- repeatable `--validation NAME=STATUS`
- repeatable `--residual`
- repeatable `--followup`
- optional `--note`
- optional `--cleanup`

When `--workset` and `--task` are omitted, `task close` infers the active task
from the current task worktree and reuses the active attempt actor. It then
delegates to the canonical non-success closure path.

### `blackdog task cleanup`

Remove a retained or leftover task workspace and delete its branch.

```bash
blackdog task cleanup --project-root /path/to/repo
blackdog task cleanup --project-root /path/to/repo --workset kernel --task KERN-1
```

Important flags:

- `--project-root`
- optional `--workset`
- optional `--task`
- optional `--path`
- optional `--branch`

When `--workset` and `--task` are omitted, `task cleanup` infers the current
task from the task worktree you are in, or falls back to the latest attempt
for that task when the attempt is already closed. This is the public same-thread
cleanup surface after `task land --keep-worktree` or after `task close --cleanup`
was skipped because the task workspace stayed dirty.

### `blackdog worktree preflight`

Show the current WTAM contract for the checkout and primary worktree.

```bash
blackdog worktree preflight --project-root /path/to/repo
blackdog worktree preflight --project-root /path/to/repo --json
```

### `blackdog worktree preview`

Preview the WTAM start plan before Blackdog claims or mutates runtime state.

```bash
blackdog worktree preview \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --prompt "Implement the kernel rewrite slice in this worktree."
```

Important flags:

- `--workset`
- `--task`
- `--actor`
- exactly one of `--prompt` or `--prompt-file`
- optional `--branch`
- optional `--from`
- optional `--path`
- optional `--model`
- optional `--reasoning-effort`
- optional `--note`
- optional `--show-prompt`
- optional `--expand-contract`

`worktree preview` is read-only. It shows:

- the planned branch, worktree path, base ref, and target branch
- prompt receipt hash and prompt source
- task paths, docs, checks, and validation defaults
- repo contract inputs such as the repo-local Blackdog skill and routed docs
- the ordered handler plan for the task worktree, including repo-root env
  validation, worktree overlay setup, source mode, launcher path, and
  remediation when start is blocked

Use `--show-prompt` when you want the exact prompt receipt text.
Use `--expand-contract` when you want the preview to inline the contract
documents Blackdog expects an agent to use.

### `blackdog worktree start`

Create a branch-backed task worktree and start the WTAM attempt for one task.

```bash
blackdog worktree start \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --prompt "Implement the kernel rewrite slice in this worktree."
```

Important flags:

- `--workset`
- `--task`
- `--actor`
- exactly one of `--prompt` or `--prompt-file`
- optional `--branch`
- optional `--from`
- optional `--path`
- optional `--model`
- optional `--reasoning-effort`
- optional `--note`

`worktree start` creates a linked worktree outside the repo, starts the typed
attempt, claims both the workset and task for `direct_wtam`, executes the
handler plan, and records:

- worktree path
- worktree-local `.VE` and `blackdog` launcher path
- task branch
- base ref / base commit
- target branch
- execution model
- prompt source
- prompt receipt hash
- handler actions and timings

On the shipped handler path, `worktree start`:

- validates the repo-root `.VE`
- creates the task worktree `.VE` from the repo-root env
- wires a site-packages overlay back to the repo-root env
- links root-bin fallback tools into the task worktree env
- writes the worktree-local `blackdog` launcher

`worktree start` never fetches from network or repairs the managed source
checkout. If the base env or managed source is missing, it fails explicitly and
points back to `blackdog repo install` or `blackdog repo update`.

### `blackdog worktree show`

Inspect the current active attempt, or the latest attempt if none is active,
for one WTAM task.

```bash
blackdog worktree show \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1
```

Important flags:

- `--workset`
- `--task`

`worktree show` is the focused recovery read surface. It reports:

- whether an active attempt still exists
- branch and target-branch identity
- task-worktree path and dirty paths
- whether the branch is ahead of target
- raw user-prompt and execution-prompt hashes, sources, and modes when captured
- primary-worktree dirtiness
- recommended next actions such as `land`, `close`, or `cleanup`

### `blackdog worktree land`

Create the canonical landed commit for the active WTAM task, close the attempt,
and clean up by default.

```bash
blackdog worktree land \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --summary "finished the slice" \
  --validation unit=passed
```

Important flags:

- `--workset`
- `--task`
- `--actor`
- required `--summary`
- repeatable `--validation NAME=STATUS`
- repeatable `--residual`
- repeatable `--followup`
- optional `--note`
- optional `--keep-worktree`

`worktree land` is the canonical success-closure surface for `direct_wtam`.
It:

- auto-stages dirty task-worktree changes and creates an internal prep commit
  on the task branch when needed
- creates one canonical landed commit for the successful task attempt
- includes one `Blackdog-Changed-Path:` trailer per changed path in that
  canonical landed commit
- records `changed_paths`, branch-head `commit`, `landed_commit`, validation
  results, and closure timing
- releases the active task/workset claims
- removes the task worktree and deletes its branch unless `--keep-worktree` is
  set

If the operational landing step cannot complete, `worktree land` closes the
active attempt as `blocked`, records the end time and note, releases the
claims, and returns a non-zero exit code. That prevents stale direct-WTAM
claims from lingering after a failed landing.

### `blackdog worktree close`

Close the active WTAM task without landing code.

```bash
blackdog worktree close \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --status blocked \
  --summary "blocked on fixture mismatch"
```

Important flags:

- `--workset`
- `--task`
- `--actor`
- required `--status blocked|failed|abandoned`
- required `--summary`
- repeatable `--validation NAME=STATUS`
- repeatable `--residual`
- repeatable `--followup`
- optional `--note`
- optional `--cleanup`

`worktree close` is the non-success closure surface for `direct_wtam`.
It records the attempt result, releases the active task/workset claims, and
preserves branch/worktree lineage for later inspection. `--cleanup` asks
Blackdog to remove the task worktree immediately, but cleanup only proceeds
when that worktree is already clean.

### `blackdog worktree cleanup`

Remove a retained or leftover WTAM worktree and delete its branch.

```bash
blackdog worktree cleanup \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1
```

Important flags:

- `--workset`
- `--task`
- optional `--path`
- optional `--branch`

`worktree cleanup` remains the lower-level WTAM operator alias. Prefer
`task cleanup` for the same-thread agent workflow. Use `worktree cleanup`
when you are operating explicitly on the WTAM worktree surface or recovering a
task workspace from outside that worktree.

### `blackdog summary`

Read the typed runtime model and print a human-oriented status summary.

```bash
blackdog summary --project-root /path/to/repo
blackdog summary --project-root /path/to/repo --workset kernel
blackdog summary --project-root /path/to/repo --json
```

### `blackdog next`

Select the next task inside one workset.

```bash
blackdog next --project-root /path/to/repo --workset kernel
blackdog next --project-root /path/to/repo --workset kernel --json
```

`next` is workset-scoped by design. It selects one task to continue or start,
and it also reports blocked tasks for that workset so recovery does not require
reading the raw snapshot by hand.

### `blackdog snapshot`

Emit the canonical machine-readable runtime snapshot.

```bash
blackdog snapshot --project-root /path/to/repo
blackdog snapshot --project-root /path/to/repo --workset kernel
```

The snapshot embeds the fully typed runtime model under `runtime_model`.
That runtime model now includes attempt/result rows from the WTAM lifecycle.

### `blackdog attempts summary`

Summarize completed attempt history from the typed runtime model.

```bash
blackdog attempts summary --project-root /path/to/repo
blackdog attempts summary --project-root /path/to/repo --workset kernel
blackdog attempts summary --project-root /path/to/repo --json
```

The summary centers on completed attempts and includes:

- recent completed attempts
- completed counts by workset
- model / reasoning-effort when present
- prompt source plus prompt hash
- commit and landed-commit linkage
- validation pass/fail/skipped totals
- landed vs not-landed completion totals

### `blackdog attempts table`

Emit a stable table over completed attempt history.

```bash
blackdog attempts table --project-root /path/to/repo
blackdog attempts table --project-root /path/to/repo --workset kernel
blackdog attempts table --project-root /path/to/repo --json
```

Text output is tab-separated with stable columns. JSON output returns the same
columns plus row dictionaries. Current columns are:

- `workset_id`
- `task_id`
- `attempt_id`
- `status`
- `actor`
- `started_at`
- `ended_at`
- `elapsed_seconds`
- `execution_model`
- `model`
- `reasoning_effort`
- `prompt_source`
- `branch`
- `target_branch`
- `start_commit`
- `commit`
- `landed_commit`
- `prompt_hash`
- `changed_paths_count`
- `validation_summary`
- `summary`

## Removed Or Deferred Commands

The old backlog-centric commands are not part of the vNext shipped surface.
That includes the markdown planning, board, inbox, and compatibility plan
commands. The rebuilt `blackdog supervisor` surface is part of the shipped
vNext CLI; the removed part is the old backlog-era supervisor tree.

Any future expansion of the supervisor/workset-manager surface must target the
same workset/task claim model and runtime snapshot foundation instead of
reviving legacy backlog flows.

If they are rebuilt later, they must target the new workset/runtime foundation
instead of reviving `backlog.md`.

Repo lifecycle workflows are different. Install/update/refresh/tune and
skill-composition flows are still first-class product concerns, but they should
live as a separate workflow family in `blackdog`, not forced into workset/task
semantics and not revived from the old scaffold command tree unchanged.
