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

```bash
blackdog init --project-root /path/to/repo --project-name "Repo Name"
```

### `blackdog repo install`

Create or repair the minimum repo-local Blackdog contract:

- repo-local `.VE`
- repo-local `blackdog` launcher
- `blackdog.toml` when missing
- explicit handler blocks when the profile still relies on synthesized defaults
- repo-local `$blackdog` skill when missing

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
artifacts through handler actions.

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

Regenerate the managed repo-local skill from `blackdog.toml`.

```bash
blackdog repo refresh --project-root /path/to/repo
```

Important flags:

- `--project-root`

`repo refresh` requires an existing `blackdog.toml`. It rewrites the repo-local
`$blackdog` skill so the skill matches the current shipped product surface and
routed-doc contract. It also validates the configured handlers and prunes known
legacy backlog-era artifacts from the shared control root.

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
- routed contract docs and the repo-local skill
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

### `blackdog worktree land`

Land the active WTAM task branch through the primary checkout and record
success/result stats.

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
- optional `--summary`
- repeatable `--validation NAME=STATUS`
- repeatable `--residual`
- repeatable `--followup`
- optional `--note`

`worktree land` derives `changed_paths`, `commit`, and `landed_commit` from the
branch being landed. It is the kept-change finish/report action for v1 and
releases the active task/workset claims when the WTAM slice is complete.

### `blackdog worktree cleanup`

Remove the landed WTAM worktree and delete its branch.

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
That includes the markdown planning, board, supervisor, inbox, and compatibility
plan commands.

Any later supervisor/workset-manager surface must target the same workset/task
claim model and runtime snapshot foundation instead of reviving legacy backlog
flows.

If they are rebuilt later, they must target the new workset/runtime foundation
instead of reviving `backlog.md`.

Repo lifecycle workflows are different. Install/update/refresh/tune and
skill-composition flows are still first-class product concerns, but they should
live as a separate workflow family in `blackdog`, not forced into workset/task
semantics and not revived from the old scaffold command tree unchanged.
