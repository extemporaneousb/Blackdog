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

### `blackdog repo bind`

Bind a repo to Blackdog-managed membership.

`repo bind` is the first-class membership name for the same contract creation
performed by `repo install`. It creates or repairs the repo-local `.VE`,
launcher, `blackdog.toml`, managed `AGENTS.md` block, and repo-local managed
skill, then reports action `bind`.

```bash
blackdog repo bind --project-root /path/to/repo --project-name "Repo Name"
blackdog repo bind --project-root /path/to/repo --source-root /path/to/blackdog
blackdog repo bind --project-root /path/to/repo --json
```

Important flags:

- `--project-root`
- optional `--project-name`
- optional `--source-root`
- optional `--json`

### `blackdog repo table`

Discover Blackdog-installed repos by scanning supplied roots for
`blackdog.toml` and emit one cross-repo membership/runtime row per resolved
repo root.

```bash
blackdog repo table --root /path/to/work --json
blackdog repo table --root /path/a --root /path/b --since 2026-05-01T00:00:00Z
blackdog repo table --root /path/to/work --since-hours 24
blackdog repo table --root /path/to/work --include-archived --no-codex
```

Important flags:

- `--root` may be repeated
- optional `--since` filters windowed attempt metrics and Codex coverage rows
- optional `--since-hours` is a convenience window, for example `24`
- optional `--include-archived`
- optional `--include-legacy-worksets` adds a legacy storage-count column for
  migration/debugging; default table output does not expose worksets
- optional `--no-codex`
- optional `--json`

Text output is stable TSV. JSON output carries the same column names under
`repo_table.rows`. Columns are:

`project_name`, `status`, `project_root`, `branch`, `dirty_count`,
`tasks_total`, `current_ready_tasks`, `current_active_attempts`,
`current_blocked_tasks`, `done_tasks_total`, `attempts_total`,
`window_attempts`, `window_problem_attempts`, `window_success_attempts`,
`window_blocked_attempts`, `window_failed_attempts`,
`window_abandoned_attempts`, `window_failure_classes`,
`window_prompt_issue_attempts`, `window_operator_issue_attempts`,
`window_elapsed_seconds`, `codex_sessions`, `codex_user_turns`,
`codex_input_tokens`, `codex_cached_input_tokens`, `codex_output_tokens`,
`codex_reasoning_output_tokens`, `codex_total_tokens`, `codex_tool_calls`,
`codex_longest_completed_turn_duration_ms`,
`codex_longest_completed_turn_started_at`,
`codex_longest_completed_turn_thread_id`,
`codex_longest_completed_turn_id`,
`implementation_like_unlinked_turns`, `linked_attempts`, `blackdog_version`,
`profile_version`, `runtime_store_version`, `support_hash`, `docs_count`,
`validation_count`, `prompt_modes`, `models`, `reasoning_efforts`, `error`.

`current_*` columns describe live task/attempt state now. `window_*` columns
describe attempts whose start or end timestamp is inside the requested window;
without `--since` or `--since-hours`, the window is all recorded attempt
history. `codex_input_tokens` is model input-token usage reported by Codex
session logs, not a tokenizer pass over only the user's message text.
`codex_longest_completed_turn_duration_ms` is the longest single Codex turn
that returned for that repo, measured from Codex `task_started` to
`task_complete`. The companion `started_at`, `thread_id`, and `id` columns
identify the turn. Codex turn metrics scan both active and archived Codex
session logs and deduplicate repeated archived snapshots by thread and turn id.

Discovery skips nested `.worktrees`, `.git`, `.VE`, `.venv`, `node_modules`,
cache, and build-output directories. `blackdog.toml` remains the source of
truth; there is no central repo registry. Archived repos are excluded unless
`--include-archived` is set. With `--no-codex`, Codex columns are null in JSON
and `-` in text.

### `blackdog repo scaffold`

Create a new git repo with the Blackdog contract installed from the start.

`repo scaffold` is for new project creation, not for converting an existing
repo in place. It can optionally use another repo as an exemplar for starter
agent docs and common routed docs, but it does not copy Blackdog runtime state
such as `.VE/`, `.codex/skills/`, `.git/blackdog/`, or `blackdog.toml`.
After creating or reusing the target git repo, it delegates to `repo install`
so the target receives the normal managed `AGENTS.md` contract block,
`blackdog.toml`, repo-local skill, and `./.VE/bin/blackdog` launcher.

```bash
blackdog repo scaffold \
  --target-root /path/to/new-repo \
  --project-name "New Repo" \
  --like /path/to/exemplar-repo \
  --dry-run

blackdog repo scaffold \
  --target-root /path/to/new-repo \
  --project-name "New Repo" \
  --like /path/to/exemplar-repo \
  --source-root /path/to/blackdog
```

Important flags:

- `--target-root`
- optional `--project-name`
- optional `--like`
- optional `--source-root`
- optional `--dry-run`

When `--project-name` is omitted, the project name is inferred from the target
directory name. `--dry-run` reports the planned target creation, git init,
seed files, and install command without mutating the target.

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
overlay `.VE` rooted at the task worktree. During worktree setup, simple
editable source paths from the repo-root env are replayed into the worktree env,
with paths inside the primary checkout remapped to the task worktree. If
`blackdog.toml` or the repo-local skill already exist, install preserves
repo-owned files and repairs runtime artifacts through handler actions. When
install has to create `blackdog.toml`,
it seeds `doc_routing_defaults` from `AGENTS.md` plus common repo docs that
already exist in the host repo so the initial contract matches the converted
repo instead of Blackdog's own docs.
If install creates or updates repo-visible managed files such as
`blackdog.toml`, `AGENTS.md`, or `.codex/skills/...`, the result includes a
note that the primary checkout remains dirty until those changes are committed,
landed, reverted, or explicitly reported. Finish lifecycle runs with
`git status --short`.

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
When update changes repo-visible managed files, it reports the same dirty
primary checkout note; `.VE` and `.git` runtime repairs alone do not trigger
that note.

### `blackdog repo refresh`

Regenerate the managed repo-local Blackdog scaffold from `blackdog.toml`.

```bash
blackdog repo refresh --project-root /path/to/repo
```

Important flags:

- `--project-root`

`repo refresh` requires an existing `blackdog.toml`. It rewrites the managed
Blackdog section in `AGENTS.md` and the repo-local managed skill at
`.codex/skills/<repo-slug>/SKILL.md` plus its `agents/openai.yaml` metadata so
both match the current shipped product surface and routed-doc contract. It
also validates the configured handlers, removes stale generated skill
auxiliary files and obsolete Blackdog-managed skill directories, and prunes
known legacy backlog-era artifacts plus the stale removed-orchestration run
directory from the shared control root.
Because refresh rewrites managed repo docs and skills, operators should expect
`git status --short` to show those files until the lifecycle change is
committed, landed, reverted, or explicitly reported.

### `blackdog repo archive`

Mark a bound repo as archived by setting `[project].status = "archived"` in
`blackdog.toml`.

```bash
blackdog repo archive --project-root /path/to/repo --reason "superseded"
blackdog repo archive --project-root /path/to/repo --json
```

Important flags:

- `--project-root`
- optional `--reason`
- optional `--json`

The reason is reported in command output only. It is not written to
`blackdog.toml`; the command updates only `[project].status`.

### `blackdog repo unarchive`

Mark a bound repo as active by setting `[project].status = "active"` in
`blackdog.toml`.

```bash
blackdog repo unarchive --project-root /path/to/repo
blackdog repo unarchive --project-root /path/to/repo --json
```

Important flags:

- `--project-root`
- optional `--json`

Missing `[project].status` still means active, but `repo unarchive` updates the
status line when the repo is currently archived.

### `blackdog repo unbind`

Preview or remove Blackdog-managed membership files from a repo.

```bash
blackdog repo unbind --project-root /path/to/repo --json
blackdog repo unbind --project-root /path/to/repo --confirm --json
blackdog repo unbind --project-root /path/to/repo --confirm --keep-control-dir
```

Important flags:

- `--project-root`
- optional `--confirm`
- optional `--keep-control-dir`
- optional `--json`

Without `--confirm`, `repo unbind` is a preview and does not mutate. It reports
planned managed-block updates, planned managed removals, preserved paths, and
unrelated dirty paths from `git status`.

With `--confirm`, unbind strips only the managed Blackdog block from
`AGENTS.md`, preserving repo-owned text. It removes `blackdog.toml`, the
current managed repo skill directory, a legacy `.codex/skills/blackdog/`
directory only when it looks Blackdog-managed, the repo-local
`.VE/bin/blackdog` launcher, and the configured control directory when that
directory is under the repo or git common dir. It preserves
`.blackdog/history.jsonl` by default. External control dirs outside the repo
and git common dir are preserved and reported as warnings.

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

Low-level planned-task authoring is disabled by default and is not part of the
normal repo skill workflow. Use `task begin`, `task show`, `task recover`, and
`task land` for ordinary kept-change work.

To deliberately repair or migrate planned task state, opt in for that command:

```bash
BLACKDOG_ENABLE_WORKSET_COMMANDS=1 \
  blackdog workset put --project-root /path/to/repo --file workset.json
```

Create or update one workset in `planning.json`.
The same payload may also carry optional `task_states` rows, which patch the
matching workset in `runtime.json`.

```bash
BLACKDOG_ENABLE_WORKSET_COMMANDS=1 \
  blackdog workset put --project-root /path/to/repo --file workset.json

BLACKDOG_ENABLE_WORKSET_COMMANDS=1 \
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

### `blackdog task begin`

Create or reuse one task envelope and start the WTAM attempt.

```bash
blackdog task begin \
  --project-root /path/to/repo \
  --actor codex \
  --prompt "Implement the same-thread slice." \
  --prompt-mode raw

blackdog task begin \
  --project-root /path/to/repo \
  --actor codex \
  --prompt-file EXECUTION_PROMPT.txt \
  --prompt-mode skill \
  --user-prompt-file USER_PROMPT.txt
```

Important flags:

- `--project-root`
- `--actor`
- exactly one of `--prompt` or `--prompt-file`
- optional `--prompt-mode raw|skill|tuned`
- optional `--user-prompt` or `--user-prompt-file`
- optional `--workset` for an existing planning task only
- optional `--task` for an existing planning task only
- optional `--title`
- optional `--branch`
- optional `--from`
- optional `--path`
- optional `--model`
- optional `--reasoning-effort`
- optional `--note`
- optional `--show-prompt`

`task begin` is the default same-thread agent entrypoint. When `--workset` and
`--task` are omitted, it creates the internal one-task execution envelope,
claims it for the caller, records both the raw user prompt receipt and the
execution prompt receipt, provisions the task worktree, and starts the WTAM
attempt in one command. That envelope is runtime bookkeeping for attempt
history and recovery, not a repo-facing planning workflow.

`--project-root` identifies the Blackdog-managed repo and control state. When
the command runs from a normal linked worktree for the same Git repository,
`task begin` treats that linked branch as the target branch and provisions a
separate task worktree that lands back to it. Running from the primary checkout
targets the primary branch. Running from an existing task worktree targets the
primary branch rather than nesting task semantics on top of that task branch.

For normal new repo work, omit `--workset` and `--task`. Those flags are only
for explicitly targeting an existing planned task; agents should not invent
them from the user request.

`--prompt-mode raw` records the supplied prompt directly. `--prompt-mode tuned`
runs the user request through `blackdog prompt tune` first and records the
tuned execution prompt as the attempt prompt receipt. The prompt receipt stores
its hash, source, and `mode` as `raw` or `tuned`. New v3 runtime rows do not
store full prompt text; they link to Codex session storage when that context is
available. `--prompt-mode skill` records the supplied prompt as a
repo-skill-composed execution prompt without running prompt tuning. When
`--user-prompt` or `--user-prompt-file` is present, Blackdog stores that raw
user request lineage separately from the execution prompt for later audit and
repo-skill optimization.

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

### `blackdog task recover`

Inspect the explicit same-thread WTAM recovery state for the current task and,
when needed, release a stale claim without reading raw snapshot state by hand.

```bash
blackdog task recover --project-root /path/to/repo
blackdog task recover --project-root /path/to/repo --workset kernel --task KERN-1
blackdog task recover \
  --project-root /path/to/repo \
  --release-stale-claim \
  --status abandoned \
  --summary "released the stale claim after an interrupted run"
```

Important flags:

- `--project-root`
- optional `--workset`
- optional `--task`
- optional `--release-stale-claim`
- optional `--status blocked|failed|abandoned`
- optional `--summary`
- optional `--note`

When `--workset` and `--task` are omitted, `task recover` infers the task from
the current task worktree and otherwise falls back to the latest attempt for
that worktree. It reports:

- the current task runtime status plus any retained task/workset claims
- whether the task is carrying a stale claim with no active attempt
- the retained task-worktree path, dirty paths, and branch-ahead state
- whether the recorded task branch and target branch still resolve locally
- primary-worktree dirtiness that would still block landing
- structured recovery fields such as `failure_class` and `recovery_action`
- recommended next actions such as `task land`, `task close`, `task cleanup`,
  or stale-claim release

If a latest historical attempt references a missing task branch or missing
target branch, recovery reads return `recovery_state="stale_reference"` with
`failure_class="stale_branch"` rather than failing on the underlying git
inspection command.

`--release-stale-claim` is intentionally narrow. It only applies when the task
claim still exists but there is no active WTAM attempt to close. In that case
Blackdog releases the lingering task/workset claim, repairs a still
`in_progress` task runtime row to `canceled` for `abandoned` or `blocked` for
`blocked`/`failed`, and leaves any retained task workspace untouched so cleanup
remains an explicit follow-on decision.

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
through `task cleanup`. The canonical landed-commit trailers are the same ones
documented under `worktree land`.

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
Closing with `--status abandoned` cancels the task so normal `summary` and
`next` views hide it.

### `blackdog task cancel`

Cancel a planned or blocked task so normal `summary` and `next` views hide it.

```bash
blackdog task cancel \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --summary "superseded by KERN-2" \
  --failure-class superseded \
  --recovery-action leave_canceled
```

Important flags:

- `--project-root`
- `--workset`
- `--task`
- optional `--actor`
- optional `--summary`
- optional `--failure-class`
- optional `--recovery-action`
- optional `--prompt-issue`
- optional `--operator-issue`

### `blackdog task reopen`

Move a canceled task back to `planned`.

```bash
blackdog task reopen \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --summary "needed again"
```

Important flags:

- `--project-root`
- `--workset`
- `--task`
- optional `--actor`
- optional `--summary`

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

Cleanup refuses to remove a workspace when the branch has uncommitted changes
or commits that Blackdog cannot prove were landed. For successful task lands,
the retained task branch may point at an internal `blackdog-wip(...)` commit
that was squash-landed into the canonical target commit. In that case cleanup
uses the recorded task-branch commit, `landed_commit`, target branch, and tree
equivalence to force-delete the local disposable task branch without requiring
operator intervention.

### `blackdog worktree preflight`

Show the current WTAM contract for the operator workspace and primary worktree.
`--project-root` identifies the managed repo and control state. If the shell's
current directory is inside another worktree for the same Git repository,
preflight reports that worktree as the current workspace; otherwise it falls
back to `--project-root`. When the current workspace is a normal linked
worktree, preflight reports that linked branch as the target branch instead of
the primary checkout branch.

The `workspace role` field is the edit rule: implementation edits belong only
in `workspace role: task`. A `primary` or `linked` workspace is a routing
context for starting a branch-backed task worktree, not an implementation
workspace.

```bash
blackdog worktree preflight --project-root /path/to/repo
blackdog worktree preflight --project-root /path/to/repo --json
```

### `blackdog worktree table`

Emit a stable table of active, dirty, retained, or otherwise cleanup-relevant
WTAM worktrees for one repo.

```bash
blackdog worktree table --project-root /path/to/repo
blackdog worktree table --project-root /path/to/repo --json
```

Text output is tab-separated with stable columns. JSON output returns the same
columns under `worktree_table.rows` plus counts for cleanup-ready, blocked,
active, and missing-worktree rows. Current columns are:

- `workset_id`
- `task_id`
- `task_title`
- `state`
- `latest_attempt_status`
- `started_at`
- `ended_at`
- `last_commit_at`
- `last_commit`
- `last_commit_message`
- `branch`
- `target_branch`
- `worktree_path`
- `worktree_dirty_count`
- `branch_ahead_of_target`
- `changed_paths_count`
- `size_bytes`
- `size`
- `cleanup_status`
- `cleanup_reason`
- `cleanup_command`
- `recommended_action`

The table is intentionally empty when there are no active or retained task
worktrees requiring operator attention. Retained worktrees whose branches are
provably represented by the canonical landed commit are marked
`cleanup_ready`. Dirty, active, missing, or unproven branch rows stay visible
with a recommended next action instead of being silently removed.

### `blackdog worktree preview`

Preview the WTAM start plan for an existing task before Blackdog claims or
mutates runtime state.

```bash
blackdog worktree preview \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --prompt "Implement the kernel rewrite slice in this worktree."
```

Important flags:

- `--project-root`
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

`worktree preview` is a low-level recovery/repair command. Use `task begin`
without `--workset` or `--task` for new work; do not invent workset or task ids.

### `blackdog worktree start`

Create a branch-backed task worktree and start the WTAM attempt for one existing
task.

```bash
blackdog worktree start \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --prompt "Implement the kernel rewrite slice in this worktree."
```

Important flags:

- `--project-root`
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

`worktree start` is a low-level existing-task command. Use `task begin` without
`--workset` or `--task` for new work; do not invent workset or task ids.

On the shipped handler path, `worktree start`:

- validates the repo-root `.VE`
- creates the task worktree `.VE` from the repo-root env
- wires a site-packages overlay back to the repo-root env
- wires worktree-local source paths for simple editable installs from the
  repo-root env
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

- `--project-root`
- `--workset`
- `--task`

`worktree show` is the focused recovery read surface. It reports:

- whether an active attempt still exists
- branch and target-branch identity
- whether the recorded task branch and target branch still resolve locally
- task-worktree path and dirty paths
- whether the branch is ahead of target
- raw user-prompt and execution-prompt hashes, sources, and modes when captured
- primary-worktree dirtiness
- structured recovery fields such as `failure_class` and `recovery_action`
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

- `--project-root`
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
- writes canonical landed-commit trailers for `Blackdog-Workset`,
  `Blackdog-Task`, `Blackdog-Attempt`, `Blackdog-Actor`, `Blackdog-Status`,
  optional `Blackdog-Target-Branch`, `Blackdog-Execution-Model`,
  `Blackdog-Model`, `Blackdog-Reasoning-Effort`, `Blackdog-Prompt-Hash`,
  `Blackdog-Prompt-Source`, and `Blackdog-Prompt-Mode`; when the raw user
  prompt lineage differs from the execution prompt lineage, it also writes
  `Blackdog-User-Prompt-Hash`, `Blackdog-User-Prompt-Source`, and
  `Blackdog-User-Prompt-Mode`; it always writes one `Blackdog-Changed-Path:`
  per changed path, plus any `Blackdog-Validation`/`Blackdog-Residual`/
  `Blackdog-Followup` trailers that were supplied at land time
- records `changed_paths`, branch-head `commit`, `landed_commit`, validation
  results, and closure timing; `commit` is the task-branch head Blackdog
  landed from, while `landed_commit` is the canonical commit created on the
  target branch
- releases the active task/workset claims
- removes the task worktree and deletes its branch unless `--keep-worktree` is
  set

If the operational landing step cannot complete, `worktree land` classifies the
failure before returning. Retryable landing blockers, such as a dirty primary
checkout, stale task branch base, or merge conflict, return a non-zero exit
code while keeping the active attempt and claims intact so the agent can fix
the blocker and rerun `worktree land` or `task land` against the same attempt.
Classified terminal failures, such as a task branch with no changes relative to
the target branch, are closed internally through the same non-success finalizer
used by `worktree close`; no separate close command is required. Use
`worktree close` or `task close` directly only when the work should be
explicitly closed as `blocked`, `failed`, or `abandoned` without attempting to
land code first.

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

- `--project-root`
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
blackdog worktree cleanup --project-root /path/to/repo --all
```

Important flags:

- `--project-root`
- `--workset` and `--task` for one task worktree
- optional `--path`
- optional `--branch`
- optional `--all` removes every row currently classified as `cleanup_ready`

`worktree cleanup` remains the lower-level WTAM operator alias. Prefer
`task cleanup` for the same-thread agent workflow. Use `worktree cleanup`
when you are operating explicitly on the WTAM worktree surface or recovering a
task workspace from outside that worktree.
`worktree cleanup --all` is the bulk cleanup companion to `worktree table`: it
deletes only rows whose cleanup proof has already passed and leaves dirty,
active, missing, or unproven rows in the table for explicit operator handling.

### `blackdog summary`

Read the typed runtime model and print a human-oriented status summary.

```bash
blackdog summary --project-root /path/to/repo
blackdog summary --project-root /path/to/repo --workset kernel
blackdog summary --project-root /path/to/repo --include-canceled
blackdog summary --project-root /path/to/repo --include-legacy-worksets --json
blackdog summary --project-root /path/to/repo --json
```

Normal summary is task/attempt first. It hides canceled tasks by default; use
`--include-canceled` for operator/debug views. JSON includes legacy workset
lists only with `--include-legacy-worksets`.

### `blackdog next`

Low-level read surface for existing planned-task state. This command is hidden
from the normal help surface because generated repo skills should use
`task begin`, task recovery, `summary`, `snapshot`, and attempt history instead
of asking agents to manage planned task queues.

Select the next task inside one existing workset.

```bash
blackdog next --project-root /path/to/repo --workset kernel
blackdog next --project-root /path/to/repo --workset kernel --json
```

`next` is workset-scoped by design. It selects one task to continue or start,
and it also reports blocked tasks for that workset so recovery does not require
reading the raw snapshot by hand. Canceled tasks are never selected.

### `blackdog snapshot`

Emit the canonical machine-readable runtime snapshot.

```bash
blackdog snapshot --project-root /path/to/repo
blackdog snapshot --project-root /path/to/repo --workset kernel
blackdog snapshot --project-root /path/to/repo --include-legacy-worksets
```

The snapshot embeds the fully typed runtime model under `runtime_model`. By
default that model leads with task and attempt rows. Legacy nested workset rows
are emitted only with `--include-legacy-worksets`.

### `blackdog attempts summary`

Summarize completed attempt history from the typed runtime model.

```bash
blackdog attempts summary --project-root /path/to/repo
blackdog attempts summary --project-root /path/to/repo --workset kernel
blackdog attempts summary --project-root /path/to/repo --include-legacy-worksets --json
blackdog attempts summary --project-root /path/to/repo --json
```

The summary centers on completed attempts and includes:

- recent completed attempts
- completed counts by task
- model / reasoning-effort when present
- user-prompt lineage and execution-prompt lineage when they differ
- stable `prompt_*` aliases only when both lineages match
- commit and landed-commit linkage
- validation pass/fail/skipped totals
- landed vs not-landed completion totals
- Codex thread/session refs when present
- structured failure fields when present: `failure_class`, `recovery_action`,
  `prompt_issue`, and `operator_issue`

Legacy per-workset summaries are emitted only with `--include-legacy-worksets`.

Here `commit` is the task-branch head Blackdog landed or closed from, while
`landed_commit` is the canonical landed commit created on the target branch
for successful WTAM closure.

### `blackdog attempts table`

Emit a stable table over completed attempt history.

```bash
blackdog attempts table --project-root /path/to/repo
blackdog attempts table --project-root /path/to/repo --workset kernel
blackdog attempts table --project-root /path/to/repo --include-legacy-worksets
blackdog attempts table --project-root /path/to/repo --json
```

Text output is tab-separated with stable columns. JSON output returns the same
columns plus row dictionaries. Current columns are:

- `task_ref`
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
- `codex_thread_id`
- `codex_session_path`
- `codex_turn_id`
- `codex_turn_started_at`
- `execution_prompt_source`
- `user_prompt_source`
- `prompt_source`
- `execution_prompt_mode`
- `user_prompt_mode`
- `prompt_mode`
- `branch`
- `target_branch`
- `start_commit`
- `commit`
- `landed_commit`
- `execution_prompt_hash`
- `user_prompt_hash`
- `prompt_hash`
- `changed_paths_count`
- `validation_summary`
- `failure_class`
- `recovery_action`
- `prompt_issue`
- `operator_issue`
- `summary`

`--include-legacy-worksets` prepends the legacy `workset_id` column for
migration/debugging.

### `blackdog codex coverage`

Compare Codex's own session/dialogue logs to Blackdog runtime attempts.

```bash
blackdog codex coverage --project-root /path/to/repo
blackdog codex coverage --project-root /path/to/repo --since 2026-05-01
blackdog codex coverage --project-root /path/to/repo --json
```

The command scans `$CODEX_HOME/sessions`, maps sessions to the repo and its git
worktrees by cwd, classifies user turns, and relates turns to Blackdog attempts
by explicit turn refs, prompt hashes, stored Codex session refs, same-session
episodes, and active attempt windows. The legacy linked/unlinked counters still
mean strong launch relationships: explicit turn refs, prompt-hash matches, or a
safe single-turn session match. Related/unrelated counters include advisory
same-session and active-window evidence so older multi-turn Codex sessions can
be analyzed retroactively without rewriting runtime state. It reports:

- Codex sessions and user-turn counts
- Blackdog attempt counts and active attempts
- linked vs unlinked turns and attempts
- related vs unrelated turns and attempts
- relationship counts such as `launch_turn`, `prompt_hash`,
  `active_attempt_window`, and `same_session`
- analysis-only turns
- unlinked implementation-like turns
- environment issue turn counts, evidence-hit counts, and structured issue
  classes such as `missing_cli`, `missing_venv`, `missing_container_runtime`,
  `missing_python_module`, `missing_node_dependency`, `missing_credential`,
  `source_file_bad_format`, and `wrong_worktree_env`
- model/reasoning observability
- longest completed Codex turn duration and turn identifiers

Coverage output may show short prompt excerpts for operator diagnosis, but it
does not persist transcript text. Environment issue evidence is extracted from
assistant/tool output and attempt summaries as bounded excerpts; it is a
read-model annotation and does not change `failure_class`.

### `blackdog codex history`

Emit compact history rows spanning Blackdog attempts and Codex user turns.

```bash
blackdog codex history --project-root /path/to/repo --jsonl
blackdog codex history --project-root /path/to/repo --since 2026-05-01 --jsonl
blackdog codex history --project-root /path/to/repo --write
```

`--jsonl` prints stable JSONL rows to stdout. `--write` writes the same rows to
`.blackdog/history.jsonl` under the project root. The file is a
cleanup and migration bridge; it is not the live source of truth.

Rows contain prompt hashes, Codex session refs, relationship labels, and bounded
environment issue evidence, not full prompts or responses. Attempt rows carry
task/result/git/validation metadata and inherit environment issue classes from
related Codex turns. Codex-turn rows cover all repo-matched user turns,
including linked Blackdog launches, same-session follow-ups, and analysis-only
work that never entered WTAM.

`execution_prompt_*` records the prompt Blackdog actually ran.
`user_prompt_*` records the raw user request Blackdog received.
The stable `prompt_*` alias is populated only when those two lineages are the
same; otherwise it is left empty so the split lineage stays explicit.

## Removed Or Deferred Commands

The old backlog-centric commands are not part of the vNext shipped surface.
That includes the markdown planning, board, inbox, compatibility-plan, and
multi-agent orchestration commands.

Any future higher-level orchestration must target the same workset/task claim
model and runtime snapshot foundation instead of reviving legacy backlog
flows.

If they are rebuilt later, they must target the new workset/runtime foundation
instead of reviving `backlog.md`.

Repo lifecycle workflows are different. `repo analyze` plus
bind/table/scaffold/install/update/refresh/archive/unarchive/unbind, prompt
tune, and attempt-inspection flows are still first-class product concerns, but
they should live as a separate workflow family in `blackdog`, not forced into
workset/task semantics. New scaffold workflows must create the current
Blackdog repo contract instead of reviving the old scaffold command tree
unchanged.
