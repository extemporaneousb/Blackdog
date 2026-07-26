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
blackdog repo table --project-root /path/to/repo --json
blackdog repo table --registry --json
blackdog repo table --root /path/to/work --since-hours 24
blackdog repo table --root /path/to/work --include-archived --no-codex
```

Important flags:

- exactly one scope mode is required: repeated `--project-root` for exact
  repos, repeated `--root` for read-only `blackdog.toml` discovery, or
  `--registry` for the user-local convenience registry
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
`implementation_like_unlinked_turns`, `linked_user_turns`,
`unlinked_user_turns`, `linked_attempts`, `unlinked_attempts`,
`cleanup_terminal_attempts`, `cleanup_retained_worktrees`,
`cleanup_landed_retained_worktrees`,
`cleanup_unlanded_terminal_attempts`, `blackdog_version`,
`managed_source_mode`, `managed_source_status`, `managed_source_head`,
`managed_source_origin`, `profile_version`, `runtime_store_version`,
`support_hash`, `docs_count`, `validation_count`, `prompt_modes`, `models`,
`reasoning_efforts`, `error`.

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
Linked/unlinked coverage columns are computed from the Codex coverage read
model over the same session rows used for the repo table. Cleanup columns count
terminal task attempts with recorded branch/worktree identity, retained
worktree paths that still exist, retained worktrees after a landed attempt, and
terminal branch/worktree attempts that have not recorded a `landed_commit`.

Discovery skips nested `.worktrees`, `.git`, `.VE`, `.venv`, `node_modules`,
cache, and build-output directories. Once a `blackdog.toml` is discovered under
a scanned root, discovery does not recurse below that repo; nested repos must be
supplied explicitly if an operator wants them counted separately.
`blackdog.toml` remains the source of truth for scanned membership. The
registry is read only when `--registry` is explicit; discovery never reads or
mutates it.
Archived repos are excluded unless `--include-archived` is set. With
`--no-codex`, Codex columns are null in JSON and `-` in text.

Managed-source columns report the configured Blackdog runtime handler state for
the repo: `managed_source_mode` is the handler source mode, status is one of
`missing`, `current`, `ahead`, `behind`, `diverged`, `no_origin`, `unknown`,
`unconfigured`, or a non-managed source mode such as `target-repo`, and the
head/origin columns show short commit ids when available.

### `blackdog local-repo`

Manage the user-local convenience registry. Registry membership is selected
only by an explicit `--registry` flag, except that bare `blackdog stats` retains
a documented compatibility fallback to the registry. `repo table` always
requires one explicit scope (`--project-root`, `--root`, or `--registry`).

```bash
blackdog local-repo add --project-root /path/to/repo
blackdog local-repo list
blackdog local-repo remove --project-root /path/to/repo
blackdog local-repo list --json
```

Important flags:

- `add --project-root` validates that the path is a Blackdog repo and records
  its resolved project root
- `remove --project-root` removes the resolved path without requiring the repo
  to still exist or still be bound
- optional `--json`

The registry is user-local state, not checked-in repo state. `BLACKDOG_HOME`
overrides its location. Without `BLACKDOG_HOME`, Blackdog uses
`$CODEX_HOME/blackdog` when `CODEX_HOME` is set, otherwise
`~/.codex/blackdog`.

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

Environment/launcher repair expectations for install are limited to the
repo-root scope: validate or create the repo-root `.VE`, write the repo-local
launcher, pin explicit handler blocks, and write the managed repo docs/skill
surfaces when needed.

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

Environment/launcher repair expectations for update are also repo-root scoped:
repair the configured source checkout/launcher path and handler-owned env
artifacts, but do not rewrite repo-owned skill text or invent task-worktree
state.

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
  --request "Round out the repo lifecycle MVP."
```

Important flags:

- `--project-root`
- exactly one of `--request` or `--request-file`
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
  --request "Round out the repo lifecycle MVP."
```

Important flags:

- `--project-root`
- exactly one of `--request` or `--request-file`
- optional `--expand-skill-text`
- optional `--expand-contract`

Text output emits the tuned prompt directly. `--json` returns the tuned prompt
plus prompt-hash and contract metadata.

`--prompt` and `--prompt-file` remain supported compatibility aliases for
`--request` and `--request-file` on both prompt commands. Canonical and
compatibility spellings use the same parser destination and preserve the
existing receipt source labels, hashes, and downstream semantics. Supplying
both spellings, or mixing an inline value with any file spelling, is an error
rather than a last-value-wins choice.

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
  --execution-prompt "Implement the same-thread slice." \
  --prompt-mode raw

blackdog task begin \
  --project-root /path/to/repo \
  --actor codex \
  --execution-prompt-file EXECUTION_PROMPT.txt \
  --prompt-mode skill \
  --request-file USER_REQUEST.txt
```

Important flags:

- `--project-root`
- optional `--actor`, default `codex`
- exactly one of `--execution-prompt` or `--execution-prompt-file`
- optional `--prompt-mode raw|skill|tuned`
- optional request lineage as `--request` or `--request-file`
- optional `--workset` for an existing planning task only
- optional `--task` for an existing planning task only
- internal replay guards `--expected-actor`,
  `--expected-execution-prompt-hash`, `--expected-execution-prompt-mode`,
  `--expected-request-prompt-hash`, and `--expected-request-prompt-mode` are
  accepted only in Blackdog-emitted exact recovery argv and hidden from
  operator help; agents execute the emitted argv unchanged rather than author
  these guards, and the guards must appear together
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

The stable ordinary task owner is `codex`. A supervising multi-agent task may
explicitly use `codex-supervisor`; that one supervisor owns the Blackdog task
and attempt while workers contribute inside it. Workers do not create parallel
Blackdog attempts or land independently.

Before it creates a new task envelope, `task begin` runs task-class startup
guards against the execution prompt. Deployment-class prompts must name the CI
or GitHub Actions route, or explicitly state an approved local/emergency
fallback, before Blackdog will create planning/runtime state. Successful starts
record a `setup_receipt` on the attempt and start event with task class,
guard probes, handler setup probes, blockers, and worktree-local runtime paths.
Post-parse setup refusals that occur before an attempt exists use the same
operation-result shape: `operation="task.begin"`, `operation_status="blocked"`,
null task/attempt status, no mutation, one blocked next action, and a bounded
`setup_guard` or `managed_skill_missing` failure code. JSON writes that result
to stdout and exits one; text renders the same action. These refusals do not
write planning, runtime, task events, or Git state. Their bounded outcome is
still recorded best-effort in the separate fail-open observability stream.

A failure later in begin is not reported as an unstructured exception when
Blackdog has retained owned state. If auto-begin created its planning envelope
and prompt artifacts but Git failed before attempt reservation, the command
returns `operation_status="partial"`, `mutation_started=true`, phase
`preflight`, the retained workset/task ids, and the executable
`retry_reserved_task_begin` action. That argv targets the same envelope and uses
absolute paths to the private replay artifacts. It preserves every supplied
`--branch`, `--from`, `--path`, `--model`, `--reasoning-effort`, and `--note`
override. Request replay remains explicit whenever the request and execution
receipts differ by hash, mode, source, or artifact identity, including
equal-text inputs from distinct roles. If an attempt was reserved, the partial
result also includes its attempt/branch/worktree identity. Its
state-derived next action is `repair_task_start_evidence` while deterministic
start evidence is missing; if a fault was raised after the final append actually
succeeded, `mutation_completed` is true and the normal active-task action is
returned instead. JSON and text exit nonzero for these partial results.

Existing-envelope resume also validates its terminal ledger boundary before
persisting prompt artifacts or previewing Git work. One exact predecessor
`task.finish` row scopes later cancel/reopen transitions by append order, even
when their `updated_at` values share one second. Duplicate matching finish rows
return `operation_status="blocked"`, `mutation_started=false`, and the terminal
`task_start_proof_required` action without creating a successor or workspace.
Only a legacy ledger with no exact finish row uses timestamp-based fallback.

`--project-root` identifies the Blackdog-managed repo and control state. When
the command runs from a normal linked worktree for the same Git repository,
`task begin` treats that linked branch as the target branch and provisions a
separate task worktree that lands back to it. Running from the primary checkout
targets the primary branch. Running from an existing task worktree targets the
primary branch rather than nesting task semantics on top of that task branch.
The `target_branch` selected and recorded by Blackdog is authoritative for
landing and verification; agents never assume it is `main` or switch it
manually.

For normal new repo work, omit `--workset` and `--task`. Those flags are only
for explicitly targeting an existing planned task; agents should not invent
them from the user request.

`--prompt-mode raw` records the supplied prompt directly. `--prompt-mode tuned`
runs the user request through `blackdog prompt tune` first and records the
tuned execution prompt as the attempt prompt receipt. The prompt receipt stores
its hash, source, and `mode` as `raw` or `tuned`. New v3 runtime rows do not
store full prompt text; they link to Codex session storage when that context is
available. `--prompt-mode skill` records the supplied prompt as a
repo-skill-composed execution prompt without running prompt tuning. A successful
skill-mode start also hashes the managed repo skill Blackdog read and records
the bounded `setup_receipt.skill_provenance` object documented in
`FILE_FORMATS.md`. The JSON result exposes the same object as
`task.skill_provenance`; the setup receipt is its canonical durable copy.
If that managed skill is missing or unreadable, a declared skill-mode start
fails before creating the task envelope or runtime state.
Raw-mode, tuned-mode, and older attempt rows have no skill provenance. When
`--request` or `--request-file` is present, Blackdog stores that raw request
lineage separately from the execution prompt for later audit and repo-skill
optimization.

Before creating planning state or reserving an attempt, a successful new begin
persists both normalized prompt receipts as content-addressed replay artifacts
under the configured shared control root. The JSON result exposes each
control-relative `*_prompt_replay_artifact_path`; runtime keeps that path with
the hash/source/mode but not the prompt text. Replay artifacts are private
local full-text files (`0600`), capped at 1,048,576 bytes, and are independent
of the original inline, stdin, or file input after begin succeeds. Task cleanup
retains them. Confirmed `repo unbind` removes them with an eligible control
directory unless `--keep-control-dir` is used. See `FILE_FORMATS.md` for the
privacy, verification, and retention contract.

Delete the agent-owned `request_file` and `execution_prompt_file` only when the
structured `task begin` result contains both a nonempty
`execution_prompt_replay_artifact_path` and a nonempty
`user_prompt_replay_artifact_path`. If either field is absent or empty, preserve
both temporary inputs as recovery evidence.

When Codex supplies `CODEX_THREAD_ID`, normal `task begin` also makes one
best-effort invocation capture in that exact thread/session. A unique current
open turn outranks a stale completed prompt-hash match; otherwise Blackdog uses
a unique prompt-hash match only when it identifies the open turn or the session
has no open turn. The attempt records `captured` with
`exact_prompt_hash`/`exact_active_turn`, or `missing` with a bounded reason.
Capture never scans unrelated sessions, never selects the latest completed
turn, and never blocks task creation.

The former execution spellings `--prompt`/`--prompt-file` and request-lineage
spellings `--user-prompt`/`--user-prompt-file` remain supported aliases. They
produce byte-identical receipt hashes, the same historical source labels, and
the same skill/setup provenance as the canonical spellings. A canonical and
legacy spelling for the same role cannot be combined, and inline/file sources
for one role remain mutually exclusive.

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
prompt lineage and the execution-prompt lineage when those differ. For an
attempt with managed-skill provenance, JSON output also exposes the canonical
setup-receipt object as top-level `skill_provenance`. Its absence means the
attempt has no recorded assertion, as expected for raw, tuned, and older rows.
If that attempt has an incomplete durable landing transaction, `task show`
also reports its transaction id, latest completed phase, and exact `task land`
next action rather than presenting the terminal runtime row as complete.

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
- any incomplete landing transaction and its latest durable phase
- any automatic stale-correction receipt, including its active/terminal
  generation and ordered phases
- the exact recorded `task land` resume argv when a correction generation was
  interrupted, or the exact worktree-local `rebase_task_branch` argv when its
  one automatic attempt was exhausted
- any retained source-worktree Git operation, which returns commandless
  `resolve_task_source_git_operation` guidance instead of guessing ownership
- recommended next actions such as `task land`, `task close`, `task cleanup`,
  or stale-claim release
- `next_action`, the authoritative typed executable command, bounded choice,
  complete state, or blocked state
- `recommended_commands`, the deprecated compatibility list; template rows
  are explicitly non-executable

For an active initial or ordinary-resume attempt whose deterministic start is
incomplete, `task show` and read-only `task recover` report
`next_action.action_id="repair_task_start_evidence"`. Execute that exact begin
argv; do not assemble a retry from diagnostic fields. A conflicting receipt,
event, path, ref, registration, or handler contract instead reports the blocked
`task_start_proof_required` action and has no executable repair argv.

If a latest historical attempt references a missing task branch or missing
target branch, recovery reads return `recovery_state="stale_reference"` with
`failure_class="stale_branch"` rather than failing on the underlying git
inspection command. A successful attempt with a recorded canonical
`landed_commit`, an existing target branch, no retained task worktree, and no
incomplete landing transaction is the exception: canonical landing normally
deletes that disposable task branch, so recovery reads report the completed
task as idle without an operator issue.

Read-only `task show` and `task recover` also opt into bounded legacy landing
detection for the exact latest failed or blocked attempt. Detection is skipped
for an active or later attempt, a task claim, a recorded landing, an abandoned
attempt, workspace-adoption evidence, or any native landing transaction. It
resolves the recorded target and exact attempt start commit, reads no more than
65 first-parent rows (64 commits after the sentinel plus the sentinel), and
reports `legacy_reconciliation_detection.state` as `ready`, `none`,
`unproven`, `ambiguous`, `inconclusive`, or `error`. Only `ready` replaces the
normal guidance with one complete read-only `task reconcile-landing` dry-run
argv. That argv never contains `--apply`; the explicit proof command is the
surface that may subsequently offer its existing guarded apply action. The
detector does not write runtime/events or mutate Git, refs, the index, or a
worktree. `task recover --release-stale-claim` is a mutation path and does not
run the detector after its write.

`--release-stale-claim` is intentionally narrow. It only applies when the task
claim still exists but there is no active WTAM attempt to close. In that case
Blackdog releases the lingering task/workset claim, repairs a still
`in_progress` task runtime row to `canceled` for `abandoned` or `blocked` for
`blocked`/`failed`, and leaves any retained task workspace untouched so cleanup
remains an explicit follow-on decision.

Stale-claim release is a durable request/decision/runtime/event transaction.
If a write is interrupted, every task read in that workset reports the owning
task and the single authoritative
`next_action.action_id="retry_stale_claim_release_finalization"`. A sibling
`task show` or read-only `task recover` therefore points back to the owner; it
does not suggest releasing the sibling. Claim-mutating `task begin`, `task
land`, and `task close` stop before workspace, Git, claim, attempt, runtime, or
event mutation until that exact retry completes. Cancel/reopen of the owning
task is also blocked. State-only transitions and landing reconciliation for a
different task remain available because they cannot change the reserved claim
projection.

The retry argv may contain the internal guards
`--stale-claim-release-request` and `--stale-claim-release-decision`. They are
hidden from help intentionally and are emitted only by Blackdog. Execute the
complete argv unchanged; never type, remove, replace, or carry these guards to
a different recovery request. An exact completed replay is a byte-for-byte
no-op. If later task/workset progress supersedes the guarded generation,
Blackdog returns the commandless
`inspect_stale_claim_release_conflict` action rather than creating a new
release generation.

Mutation output distinguishes durable progress from full completion.
`released_stale_claim` and `stale_claim_release_runtime_finalized` become true
only after the runtime replacement is durable;
`stale_claim_release_event_finalized` becomes true only after all owned release
events are durable; and `stale_claim_release_finalization_pending` stays true
until both stores agree. Partial results expose these fields and the exact retry
action instead of claiming the release either wholly failed or wholly landed.

#### Normal task result and next-action contract

Every recognized lifecycle outcome from a normal task command (`begin`, `show`,
`recover`, `land`, `reconcile-landing`, `close`, `cancel`, `reopen`, and
`cleanup`) returns the same typed operation result in JSON and renders that
same result in text. Malformed invocations, unknown identities, and caller
identity conflicts remain fatal command errors on stderr; they are not durable
recovery states and do not synthesize a `next_action`. A structured result
identifies the operation and its status, post-operation task and attempt status,
post-operation disposition, whether mutation started and completed, the last
mutation phase, an optional bounded failure code, and exactly one `next_action`.

`next_action.kind` is one of:

- `command`: one complete executable `argv`; optional `alternatives` are also
  complete commands
- `choice`: a bounded list of complete executable `choices`, with no implicit
  default command
- `complete`: no command because no lifecycle action remains
- `blocked`: no command because proof or repair is required first

For executable actions, `argv` is authoritative and `command` is its exact
shell-quoted rendering. `display` is explanatory text, never a command. Text
output prints the same action id, kind, disposition, display, and exact command
that JSON reports. Values containing whitespace, quotes, or a leading dash are
kept as single arguments. Placeholder text, status alternatives, and missing
prompt files are never emitted as executable `argv`.

Normal task text renders the operation result and authoritative `next_action`
before diagnostic state. It does not print the compatibility
`recommended_actions` or `recommended_commands`; those fields remain available
in JSON only for older consumers.

The older `recommended_actions` and `recommended_commands` keys remain
additive compatibility views. A legacy row containing placeholders is marked
`template=true`, `deprecated=true`, and `executable=false`; agents must execute
only `next_action.argv` (or one complete choice/alternative argv). In
particular, the legacy `task begin --prompt "..."` row is never selected as a
next action.

For every structured result from `task begin`, `task show`, `task recover`,
`task cancel`, `task reopen`, `task land`, `task reconcile-landing`, `task
close`, or `task cleanup`, agents must treat `next_action` as the sole authority
regardless of `operation_status`. Execute its exact `argv` for `kind=command`,
select only a complete bounded choice or alternative when offered, and stop
when `kind=blocked` or `kind=complete`. Never infer an action from display text,
error or reason prose, summaries, or compatibility recommendations.

Retained-workspace adoption is an internal `task begin` recovery route, not a
command an agent assembles. `task show` emits the complete guarded argv only
for the exact latest `abort_complete` predecessor whose source worktree and
branch are still clean, registered, and unchanged. The argv binds predecessor,
abort transaction, source commit/tree, branch/path, target branch/commit,
actor, both prompt lineages, skill provenance, setup receipt, and handlers.
Blackdog rechecks target immediately before reserving the deterministic
successor: candidate containment still belongs to predecessor reconciliation;
other target drift produces a fresh guarded adoption action with no mutation.
Once reserved, missing deterministic core or `worktree.start` evidence routes
back to the same exact begin argv before land, reconcile, close, or cancel may
terminalize the successor.

The same rule applies to ordinary same-envelope resumes and recoverable initial
starts. Exact begin repair first validates the recorded canonical primary and
task-worktree paths, branch registration/tip/HEAD, clean status, prompt lineage,
runtime claims, and current handler plan without mutation. It may recreate only
a missing owned workspace and branch at the recorded start commit. It never
moves a branch, adopts an alternate registration, accepts a symlink spelling,
or rewrites conflicting evidence. Handler execution begins only after the
read-only contract check passes. Close, cancel, and land reject an incomplete
start before runtime, Git, or cleanup mutation. Concurrent exact retries are
serialized by the attempt lock; a completed retry reports a reused workspace
and is a true runtime/event no-op.

For an active adopted successor, `task show` reports the live relation to its
target. `behind` and `diverged` route to the exact worktree-local rebase;
equal/ahead continue normally. If the predecessor candidate arrives and there
is no successor-only work, special reconciliation can finish the successor
from the original source or a bounded, no-merge, stable-patch-equivalent
rebase. Otherwise `task land` owns successor work through its normal landing
transaction. Both paths persist `worktree.adoption.completion.intent` before
runtime finalization and `worktree.adoption.complete` before source cleanup.
`task show` prioritizes the exact completion repair after a crash; exact
retries converge and a fully completed retry is a true no-op. Agents must not
invent `--adopt-aborted-landing-source` or any `--expected-*` adoption guard.

Recovery preserves the existing envelope. A terminal attempt without a
retained workspace receives a `task begin` command only after Blackdog re-reads
the recorded execution file and, when distinct, request file and proves their
normalized hashes and modes match the attempt receipts. The command reuses the
existing workset, task, persisted actor, prompt mode, and exact file lineage. It
also carries expected actor and lineage values so a file or task-state change
after action emission is rejected before any attempt, worktree, branch, or
runtime mutation. Existing-envelope `task begin` always validates the incoming
actor and prompt receipts against the latest terminal attempt at that boundary,
so omitting the expected-value flags cannot bypass the check. It never
synthesizes prompt text. Inline/stdin sources, missing or changed files,
missing actor attribution, and otherwise inexact lineage produce a blocked
action with `required_inputs` and no executable argv. A canceled task receives
`task reopen` first. For tasks with no attempt, cancel/reopen actor attribution
is recovered from the durable `task.cancel`/`task.reopen` event rather than a
process-local default. Cleanup of a terminal clean workspace is a separate
action before resume. Missing branch metadata, missing refs, and Git reference
or relationship inspection errors expose typed evidence and a blocked repair
action rather than falling through to landing. Missing refs and Git command
errors are distinct states; a command error is never accepted as absence for
cleanup or reconciliation proof.

Start recovery never copies prompt text into events, error messages, or
diagnostic commands. Runtime/event receipts contain hashes, modes, provenance,
and private control-relative artifact references, not full prompt bodies. An
executable recovery argv may contain the absolute local artifact filename so it
works from any current directory; those files remain mode `0600` beneath the
mode `0700` control hierarchy and are verified by hash before use.

Mutation reporting distinguishes a completed command from a partial one. In
particular, `task close --cleanup` can finish runtime closure while leaving a
dirty or otherwise unremovable workspace; that result is `partial`, reports
`mutation_completed=false`, and uses phase
`runtime_finalized_cleanup_pending`. A cleanup that proves the workspace and
branch already absent reports no mutation rather than claiming a filesystem
write. If workspace removal succeeds but branch deletion fails, cleanup returns
structured `partial` output with phase
`worktree_removed_branch_cleanup_pending`; its next action retries the exact
cleanup, and rerunning that action is idempotent. Cleanup evidence uses a
deterministic append-once event. If Git/filesystem cleanup finishes but the
event write is unconfirmed, the result is a structured partial with phase
`cleanup_event_finalization_pending` and an exact event-finalization retry;
whether the first write failed before or after append, retries converge on one
event and later retries are true no-ops. Text output calls a true no-op `already
clean` rather than claiming the workspace was removed.

### `blackdog task land`

Land the current task and close it.

```bash
blackdog task land \
  --project-root /path/to/repo \
  --summary "finished the same-thread slice" \
  --validation unit=passed
```

Important flags:

- `--project-root`
- optional `--workset`
- optional `--task`
- optional `--actor`
- `--summary`; a nonblank value is required for the first landing request and
  may be omitted only when replaying an existing immutable transaction
- required repeatable `--validation NAME=STATUS`; provide at least one row with
  status `passed`, `failed`, or `skipped`
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
When re-entering an incomplete transaction, the exact next-action command
supplies its workset/task identity and resolves the recorded attempt even if
runtime finalization has released the claim or task cleanup has removed the
original cwd.

An active task with dirty or branch-ahead work does not fabricate closeout
evidence. `task show` and read-only `task recover` return blocked action
`landing_evidence_required`, no argv, and required inputs
`completion_summary` and `validation_evidence`. The first `task land` call also
returns that blocked result unless its summary is nonblank and it carries at
least one validation row. This refusal reports mutation phase `none` and does
not create a landing transaction, append canonical events, update runtime, or
mutate Git. Record an honest `skipped` row when a named check was deliberately
not run; Blackdog never invents one.

If that evidence-bearing first request discovers that the task branch is not
based on its recorded target, the blocked result carries the exact
worktree-local `rebase_task_branch` argv as authoritative `next_action` by
default. When the versioned `[landing]` policy enables
`automatic_stale_rebase`, `task land` instead records an append-once correction
receipt, runs that exact `git rebase --autostash` once in the task worktree,
and executes every authoritative `[taxonomy].validation_commands` entry there.
Commands use `/bin/sh -c`, have the configured per-command timeout, stop at the
first failure, and retain only bounded typed results and hashes, not command
output.

A clean correction appends the reserved
`blackdog-post-rebase-validation=passed` evidence row and retries the existing
canonical landing transaction. Conflict, failed validation, or an unsafe
workspace returns a commandless typed blocker and retains the task workspace
for the current landing agent. Blackdog never chooses ours/theirs, resets,
force-updates, or skips checks. If the target advances a second time before
intent, the automatic loop stops, records `retry_exhausted`, and returns the
existing exact command-bearing `rebase_task_branch` action. Staleness after
landing intent remains owned by the existing transaction/CAS recovery path.
The low-level `worktree land` command never opts into this correction.

Landing is a durable transaction keyed to the attempt. Before its first Git
mutation, Blackdog appends an immutable intent that binds the complete request,
including actor, summary, validation/residual/follow-up rows, note, cleanup
choice, source lineage, and expected target base. It then records the ordered
phases `intent_recorded`, `source_prepared`, `canonical_commit_created`,
`target_updated`, `temporary_cleanup_complete`, `runtime_finalized`,
`land_event_recorded`, `task_cleanup_complete`, and `complete`. A retry with
different intent inputs is blocked rather than silently replacing the first
request.

An interrupted normal landing path with outcome `landing_in_progress` is
resumed by running `task land` again for the same attempt, even when runtime
finalization has already released its active claim. `next_action.argv` carries
the exact recorded landing request, so agents do not reconstruct summary or
closure evidence from prose. Each retry verifies the durable phase prefix and
the corresponding Git/runtime/event postconditions before continuing. A
completed retry is a no-op: it does not create another canonical commit,
rewrite runtime, or append duplicate transaction events.

The public task surface may also be invoked with no closeout fields once that
transaction exists; Blackdog reuses the complete immutable request rather than
requiring the caller to resupply evidence. Supplying replacement fields still
must match the recorded intent exactly.

The transaction outcome is `landing_in_progress`, `landed_complete`,
`abort_in_progress`, or `abort_complete`. `landing_in_progress` routes to the
exact recorded `task land`; `abort_in_progress` routes to the exact recorded
`task close`; a pre-finalization superseded abort resumes the normal landing
ledger. Terminal outcomes use the post-operation task state, except that an
`abort_complete` whose exact recorded candidate later reaches the target can
offer bounded reconciliation proof. These outcome-derived actions are
authoritative even when runtime already looks terminal.

The target branch advances only when it still matches the base captured by the
intent, or when the recorded canonical landed commit is already represented on
the target. Landing does not implicitly pull, reset, or overwrite a target that
advanced independently. The task worktree and branch are preserved through
runtime finalization and the append-once `worktree.land` event; source cleanup
is the final phase unless `--keep-worktree` records intentional retention.

`task land` uses process exit status as an automation contract: only a
structured `operation_status="succeeded"` result exits zero. Retryable blockers
(including dirty-primary, stale-branch, and Git-proof failures) and terminal
closed outcomes such as no changes exit nonzero while preserving the full JSON
result on stdout. An interrupted transaction returns a structured partial
result whose mutation phase is the latest durable phase prefixed with
`landing_` (for example, `landing_target_updated`) and whose single executable
next action is the exact resume command. Agents must inspect the structured
result instead of treating JSON emission itself as success.

Supervised integration closeout should use `task land` for successful worker
slices and pass validation rows, residual risks, and follow-up candidates as
explicit closure data. The final report should name the ownership slice and
changed files, then point to these recorded closeout fields instead of relying
only on chat context.

### `blackdog task reconcile-landing`

Prove an already-landed canonical Blackdog commit and optionally correct a
latest terminal attempt whose Git landing succeeded before runtime
finalization.

```bash
blackdog task reconcile-landing \
  --project-root /path/to/repo \
  --workset WORKSET \
  --task TASK \
  --attempt ATTEMPT \
  --landed-commit COMMIT \
  --actor ledger-auditor \
  --reason "repair post-Git finalization" \
  --apply
```

The command is a dry run unless `--apply` is present. All task identities and
the commit are required; it never infers a latest task from cwd. The ordinary
compatibility path requires the latest failed or blocked historical attempt,
with no active claim, later attempt, or recorded landing. The commit must be
reachable from the recorded target branch and carry exact canonical workset,
task, attempt, success-status, target-branch, actor, and changed-path evidence.
When the recorded source commit still exists, Blackdog also requires stable
patch equivalence.

`--actor` identifies the operator performing the audit correction; it does not
replace the attempt actor or the commit actor. A commit/attempt actor mismatch
is rejected unless the same attempt's existing terminal events explicitly
record the post-Git actor-ownership finalization failure. Apply changes no Git
state. It corrects the runtime attempt to success, records the landed commit,
marks the task done, clears failure flags, preserves existing attempt evidence,
and appends one idempotent `task.landing.reconciled` event. Existing failure and
close events are never rewritten or replaced. Re-running the same proven
correction is safe and repairs a missing reconciliation event if a prior process
stopped after the runtime write.
Mutation reporting distinguishes runtime repair, event-only repair, and a
combined repair with phases `runtime_finalized`, `event_finalized`, and
`runtime_and_event_finalized`. An event-only retry therefore reports a real
mutation even though runtime was already correct.

This remains the explicit proof/apply compatibility surface for historical
attempts. Direct read-only task/worktree recovery reads may automatically find
one candidate only inside the bounded first-parent window described above.
They do not apply it: `ready` emits this command's complete dry-run argv with
the exact project/workset/task/attempt/commit/attempt-actor identity and a
bounded reason, never `--apply`. Zero plausible identity commits is `none`;
multiple plausible commits is `ambiguous`; malformed singleton proof is
`unproven`; an absent bounded start sentinel is `inconclusive`; and an
operational Git inspection failure is `error`. Any native landing transaction
is excluded from this legacy scan: its structured action remains exact `task
land`, or exact `task close` after durable abort intent.

The narrow native exception is a terminal `abort_complete` whose exact recorded
canonical candidate later becomes reachable from the target. Reconciliation
must use that candidate, verify the complete abort chain, and prove the retained
source or exact independently authorized cleanup evidence. This path applies to native
blocked/failed aborts and is the only eligibility for an abandoned attempt;
arbitrary abandoned historical rows do not qualify. Apply appends the exact
transactional `worktree.land` evidence and honors the original landing cleanup
choice through supported task cleanup. Apply shares the attempt operation lock
with landing, close, and cleanup.

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

For an ordinary active attempt, close is one durable transaction shared by
`task close`, `worktree close`, and terminal pre-intent `task land` failure
classification. Before core runtime or Git mutation, Blackdog records an
immutable `worktree.close.request` that binds the exact attempt, actor,
terminal evidence, source projection, cleanup ownership decision, and derived
core/cleanup/receipt ids. It then converges core finalization, performs only an
exact authorized cleanup, and appends the deterministic `worktree.close`
receipt. A retry after any interruption resumes that generation; a third retry
after completion is a zero-write no-op.

Partial close results and `task show`/read-only `task recover` expose
`next_action.action_id=retry_task_close_finalization`. Its argv contains a
hidden `--close-request` capability and the exact recorded semantics. Execute
that argv without adding, dropping, or editing arguments. The hidden guard can
hydrate every omitted close field, but it is machine output rather than an
operator-authored flag. If durable semantics, source ownership, or a successor
conflict with an incomplete predecessor, `next_action.kind=blocked` has no
argv; inspect the evidence instead of constructing another close command.

While this transaction is incomplete, same-task begin/reopen/cancel/land,
cleanup, and reconciliation mutations route back to the exact close action.
Other tasks and worksets are not gated. Once the terminal receipt is verified,
the gate disappears. If a successor already exists, only a fully complete
predecessor may be replayed, and only as a verified no-op.

`--cleanup` is a request, not removal authority. Blackdog removes only the
frozen exact task path/ref when registration, branch, HEAD, cleanliness, and
landed or patch-equivalent disposition all prove it safe. A dirty, detached,
primary, moved, foreign, or unlanded source is retained with explicit proof in
the close receipt; core closure still completes and no other ref or worktree is
moved or force-deleted.

Close is serialized with landing by the attempt operation lock. A normal
transaction that already updated the target cannot be converted to non-success;
its authoritative action resumes exact `task land`. Before target update,
`task close` may create a durable abort. The abort binds the complete close
request, including actor, status, summary, validations, residuals, follow-ups,
note, cleanup choice, failure fields, issue flags, and deterministic core
finalization id. A conflicting close retry is rejected; a partial abort returns
the exact recorded `task close` argv.

The six durable product event types are `worktree.landing.abort`,
`worktree.landing.abort-cleanup`, `worktree.landing.abort-superseded`,
`worktree.landing.abort-runtime-finalized`,
`worktree.landing.abort-close-event-recorded`, and
`worktree.landing.abort-complete`. Their paths are strictly ordered. The
terminal path is
`worktree.landing.abort` -> `worktree.landing.abort-cleanup` ->
`worktree.landing.abort-runtime-finalized` ->
`worktree.landing.abort-close-event-recorded` ->
`worktree.landing.abort-complete`. The only alternative is
`worktree.landing.abort` -> `worktree.landing.abort-cleanup` ->
`worktree.landing.abort-superseded`: if the target contains the exact canonical
candidate before the abort's core `task.finalization.request` is durable,
normal landing resumes. The core request is the point of no return; after it
exists, late target containment cannot supersede close.

Abort cleanup removes only the deterministic temporary landing worktree. The
task source worktree and branch remain retained even when `--cleanup` was
requested. After core finalization converges runtime and its owned events,
Blackdog records the abort runtime stage, appends the deterministic
`worktree.close`, records the close-event stage, and records `abort-complete`.
Runtime may be terminal before those last event stages; keep executing the
structured exact-close `next_action` until the transaction outcome is
`abort_complete`. Abort completion alone is not proof that unique retained
work is disposable: `task cleanup` still refuses an unlanded branch. Cleanup
requires normal landed/patch-equivalent proof or exact adopted-successor
completion; otherwise the retained source remains the recovery asset.

### `blackdog task cancel`

Cancel a planned or blocked task so normal `summary` and `next` views hide it.

```bash
blackdog task cancel \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor lifecycle-agent \
  --summary "superseded by KERN-2" \
  --failure-class superseded \
  --recovery-action leave_canceled
```

Important flags:

- `--project-root`
- `--workset`
- `--task`
- required `--actor`
- optional `--summary`
- optional `--failure-class`
- optional `--recovery-action`
- optional `--prompt-issue`
- optional `--operator-issue`

Cancel is crash-safe across its runtime/event boundary. If deterministic
request or decision reservation, runtime replacement, or the owned
`task.cancel` event is interrupted, the command returns a typed partial result
and one exact `retry_task_cancel_finalization` action. Run that argv unchanged;
it repairs the matching durable stage without creating a second cancellation
generation. Blackdog adds internal `--transition-request` and, once durable,
`--transition-decision` guards to that generated argv. They are repair
identities, not operator-authored workflow flags: a completed exact identity is
a no-op, while an action superseded by reopen or another lifecycle generation
returns a commandless proof-required conflict. A retry with different actor,
summary, or failure fields is also a hard conflict.

### `blackdog task reopen`

Move a canceled task back to `planned`.

```bash
blackdog task reopen \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor lifecycle-agent \
  --summary "needed again"
```

Important flags:

- `--project-root`
- `--workset`
- `--task`
- required `--actor`
- optional `--summary`

Reopen has the same crash-safe contract. An interrupted transition returns one
exact `retry_task_reopen_finalization` action, repairs only its matching
runtime/event stage, rejects changed retry semantics, and becomes a durable
no-op once complete. Its generated argv carries the same internal transition
identity guards, so a saved reopen action cannot reopen a task again after a
later legitimate cancel.

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
operator intervention. Closed attempts that were landed outside the canonical
Blackdog landing path may lack `landed_commit`. Cleanup also accepts those
branches when the attempt metadata still identifies the branch and target,
the branch contains no merge commits, and every branch patch is independently
reported as equivalent to a patch already on the target. Mixed or unproven
patch sets remain blocked.

`task close --cleanup` and `task cleanup` exit nonzero for structured partial
results. This leaves the JSON payload available to automation while preventing
an incomplete filesystem mutation from being mistaken for success.
Cleanup shares the attempt operation lock with landing, close, and
reconciliation apply. It never removes the source worktree or branch required
by an incomplete landing transaction. Abort cleanup is specifically temporary
landing-worktree cleanup. `abort_complete` alone is not disposability proof:
`task cleanup` refuses an unlanded retained source so adoption remains possible.
Source removal requires ordinary landed or patch-equivalent proof, or exact
validated adoption-completion proof, and records deterministic
`worktree.cleanup` evidence only after that independent authorization succeeds.

### `blackdog worktree preflight`

Show the current WTAM contract for the operator workspace and primary worktree.
This is optional read-only diagnosis, not a prerequisite for normal
implementation. `task begin` is the normal implementation entrypoint and runs
its own readiness checks before returning the task workspace.
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

Task-class guard extension points may consume this diagnosis instead of
expanding it. `worktree preflight` reports whether the checkout is an allowed
implementation workspace and whether the normal WTAM landing path is ready;
`task begin` owns enforcement for the normal start path.
Deployment, credential, external-service, or approval checks belong in
product-layer task/repo-skill guard code or a future guard command that can use
preflight output as one input.

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
provably represented on the target branch are marked `cleanup_ready`. The
proof may come from the canonical landed-commit record or independent
patch-equivalence checks for a terminal attempt. Dirty, active, missing, or
unproven branch rows stay visible with a recommended next action instead of
being silently removed.

### `blackdog worktree preview`

Preview the WTAM start plan for an existing task before Blackdog claims or
mutates runtime state.

```bash
blackdog worktree preview \
  --project-root /path/to/repo \
  --workset kernel \
  --task KERN-1 \
  --actor codex \
  --execution-prompt "Implement the kernel rewrite slice in this worktree."
```

Important flags:

- `--project-root`
- `--workset`
- `--task`
- `--actor`
- exactly one of `--execution-prompt` or `--execution-prompt-file`
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
  --execution-prompt "Implement the kernel rewrite slice in this worktree."
```

Important flags:

- `--project-root`
- `--workset`
- `--task`
- `--actor`
- exactly one of `--execution-prompt` or `--execution-prompt-file`
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
- setup receipt status, task class, guard probes, handler probes, blockers,
  and prepared worktree-local runtime paths

`worktree start` applies the same task-class startup guard as `task begin`.
Deployment-class prompts must name the CI/GitHub Actions route or explicitly
approve local/emergency fallback before Blackdog starts the attempt.

Both low-level commands retain `--prompt` and `--prompt-file` as supported
compatibility aliases. Canonical and compatibility spellings produce the same
execution receipt and cannot be combined or mixed across inline/file sources.

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

Environment/launcher repair expectations for start are task-worktree scoped:
create the worktree-local `.VE`, wire overlays and source paths, link root-bin
fallback tools, and write the worktree-local launcher. Source-checkout repair
stays with repo lifecycle commands.

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
- any incomplete landing transaction and its outcome-derived exact `task land`
  or `task close` resume action
- bounded, read-only legacy landing detection for direct CLI `worktree show`
- recommended next actions such as `land`, `close`, or `cleanup`

The detection contract is identical to direct read-only `task show`/`task
recover`: 64 first-parent commits after the exact start sentinel, typed
`ready|none|unproven|ambiguous|inconclusive|error` evidence, and at most one
read-only dry-run reconciliation action without `--apply`. Internal
`inspect_task_worktree` calls remain opt-in false so tables and post-mutation
reads do not pay for or act on this compatibility scan.

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

These effects are executed through the same durable attempt transaction used
by `task land`. Intent is persisted before source preparation, a deterministic
temporary worktree creates the canonical commit, and the target update uses the
recorded base as a compare-and-swap guard. The command does not implicitly pull
or reset. Runtime and append-once `worktree.land` evidence are finalized before
the task source is removed. If a process stops before runtime finalization,
`worktree land` can be retried against the still-active attempt. During the
normal phase ledger, exact `task land` is the authoritative resume path,
including after claims are released. If close has durably branched the
transaction into abort, exact `task close` becomes authoritative instead.
Blackdog verifies the recorded phase or abort postconditions and continues
without creating a second landed commit or a conflicting terminal result.

If the operational landing step cannot complete, `worktree land` classifies the
failure before returning. Retryable blockers found before durable finalization,
such as a dirty primary checkout, stale task branch base, or merge conflict,
return a non-zero exit code while keeping the active attempt and claims intact
so the agent can fix the blocker and rerun `worktree land` or `task land`
against the same attempt. An interruption after runtime finalization may already
have released those claims; the transaction ledger still allows only that same
attempt and immutable request to re-enter and finish.
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
For an ordinary attempt it uses the same schema-v1 close request, core
finalization verifier, exact cleanup owner checks, deterministic receipt, and
recovery gate as `task close`. `--cleanup` proceeds only when the frozen task
path, registration, branch, source HEAD, cleanliness, and safe branch
disposition all match. Otherwise the source is explicitly retained and close
still finishes. A partial result emits the canonical guarded `task close`
action so recovery has one surface and one semantic owner.

When landing already has a nonterminal transaction, this alias follows the
same abort contract as `task close`: before target update it records an
immutable close request and retains the source; after abort intent, every
partial result resumes the exact structured `task close` action. If target
update already belongs to the normal landing ledger, the result resumes exact
`task land` instead. Do not infer either command from the error prose.

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

### `blackdog stats`

Report task, attempt, and Codex-session metrics directly without composing
`repo table`, `summary`, and Codex coverage by hand.

```bash
blackdog stats --project-root /path/to/repo
blackdog stats --project-root /path/a --project-root /path/b --since 2026-06-01 --until 2026-06-20
blackdog stats --root /path/to/work --since 2026-06-01 --json
blackdog stats --root /path/a --root /path/b --timezone America/Los_Angeles
blackdog stats --registry --json
blackdog stats --project-root /path/to/repo --by day --timezone America/Los_Angeles --json
blackdog stats --project-root /path/to/repo --tsv
```

Important flags:

- optional repeated `--project-root` for exact repo selection
- optional repeated `--root` to scan explicitly supplied filesystem roots for
  `blackdog.toml`
- optional `--registry` to select the user-local convenience registry
- the three scope modes are mutually exclusive; bare `stats` retains the
  historical registry fallback and reports a compatibility note asking callers
  to pass `--registry` explicitly
- optional `--since` and `--until` as ISO timestamps or `YYYY-MM-DD` dates
- optional `--by day`; day is the current shipped bucket granularity
- optional `--timezone`, for example `America/Los_Angeles`
- optional `--json`
- optional `--tsv`

JSON from both `stats` and `repo table` reports `scope_source`,
`supplied_roots`, selected `project_roots`, `deduped_project_roots`, and bounded
`scope_evidence`/`scope_notes`. Discovery is read-only and does not register
repos. The registry is an operator convenience list, never repository truth.
Both commands resolve candidate profiles through the same canonicalization
contract, so repo descendants and other aliases report identical selected and
deduped roots. Discovery and registry scopes retain valid repos when another
candidate is malformed, missing, or stale and report that candidate as bounded
`profile_error` evidence; stats errors only when no usable repo remains. Exact
`--project-root` selection is strict and errors if any supplied candidate is
not a usable Blackdog repo.

JSON includes `lifecycle_observability` at fleet level and in each repo row.
This is bounded health for the optional product-layer observation stream, not
proof that every lifecycle operation was observed. `stream_health` is
`missing` when no stream existed before the stats read, `degraded` when the
reader found duplicates, malformed or unknown rows, capacity pressure,
truncation, unreadable data, or process-local failed writes, and `healthy` only
when the existing stream was readable, contained unique known valid rows, and
retained at least one maximum-row slot of headroom. Observation, surface,
outcome, reason, label, duplicate, capacity, missingness, and failure counts
remain available even when the stream is absent or damaged. Stats reads the
existing health first and only then best-effort stamps its own `stats.read`
observation, so a fresh repo cannot manufacture a healthy result by being
inspected. Text output renders every bounded outcome count, including
`partial`, rather than collapsing incomplete mutations into failures.

Date-only `--since` resolves to local midnight in the selected timezone.
Date-only `--until` is inclusive by date and resolves to the next local
midnight. Attempt outcome metrics and Codex turn metrics are filtered by
`started_at` inside that window; current task/attempt metrics describe the
latest runtime state.

Metric definitions:

- `tasks_total`: all task runtime rows
- `current_tasks`: tasks whose current runtime status is neither `done` nor
  `canceled`
- `current_done_tasks`: tasks whose current runtime status is `done`
- `current_blocked_tasks`: tasks whose current runtime status is `blocked`
- `canceled_tasks`: tasks whose current runtime status is `canceled`
- `attempts_total`: all attempt rows
- `current_attempts`: attempts whose status is active
- `completed_attempts`: attempts in the selected window whose status is not
  active
- `success_attempts`, `abandoned_attempts`, `blocked_attempts`, and
  `failed_attempts`: completed attempt counts by final status in the selected
  window
- `landed_attempts`: completed attempts in the selected window with
  `landed_commit`
- `not_landed_attempts`: completed attempts in the selected window without
  `landed_commit`
- `cleanup_terminal_attempts`: terminal attempts with recorded branch or
  worktree identity
- `cleanup_retained_worktrees`: terminal attempts whose recorded worktree path
  still exists
- `cleanup_landed_retained_worktrees`: retained worktree paths for terminal
  attempts that also recorded a `landed_commit`
- `cleanup_unlanded_terminal_attempts`: terminal branch/worktree attempts
  without a recorded `landed_commit`
- `codex_user_turns`: repo-matched Codex user turns in the selected window
- `codex_linked_user_turns` and `codex_unlinked_user_turns`: Codex user turns
  with or without a strong Blackdog attempt relationship
- `codex_implementation_like_unlinked_turns`: implementation-like Codex turns
  that did not strongly link to a Blackdog attempt
- `codex_linked_attempts` and `codex_unlinked_attempts`: Blackdog attempts with
  or without a strong Codex turn relationship in the selected window
- `codex_tool_calls`: tool/function-call items in those turns
- `codex_*_tokens`: token counters reported by Codex session logs for those
  turns

Bucket rows are keyed by attempt or Codex turn `started_at` converted to the
selected timezone. Explicit, discovered, and registered project roots are
deduplicated by their resolved Blackdog profile project root. Root discovery is
read-only, does not update the registry, stops below a discovered repo, and
uses the same generated/worktree-directory exclusions as `repo table`. A nested
repo with its own `blackdog.toml` is counted separately when it is independently
in scope; path aliases for the same repo collapse into one repo row.

Stats uses each target repo's project root and git worktree roots to prune
unrelated Codex session logs before full parsing or cache materialization. An
attempt's captured exact thread/session/turn reference may add only that source
turn even when its session cwd belongs to another repo; a legacy reference may
do so only through one unique hash match in its exact session. Per-repo rows
retain those relationships, while fleet session, user-turn, tool-call, token,
and day-bucket metrics deduplicate shared turns by `(thread_id, turn_id)`.
It also uses a lightweight Codex parse mode that skips environment-issue
evidence extraction because stats reports aggregate task/attempt/Codex counters,
not diagnostic evidence excerpts. Use `blackdog codex coverage` or
`blackdog codex history` when environment issue evidence is the subject.

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
- `codex_capture_status`
- `codex_capture_method`
- `codex_capture_missing_reason`
- `execution_prompt_source`
- `user_prompt_source`
- `prompt_source`
- `execution_prompt_replay_artifact_path`
- `user_prompt_replay_artifact_path`
- `prompt_replay_artifact_path`
- `execution_prompt_mode`
- `user_prompt_mode`
- `prompt_mode`
- `skill_path`
- `skill_hash`
- `skill_source`
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

The `skill_*` columns flatten `setup_receipt.skill_provenance` for skill-mode
attempts: `skill_path` maps to `path`, `skill_hash` to `sha256`, and
`skill_source` to `source`. They are empty for raw-mode, tuned-mode, and older
attempts. The digest identifies the managed skill bytes Blackdog associated at
task start; it is provenance evidence, not an attestation that a model consumed
or followed the skill.

`--include-legacy-worksets` prepends the legacy `workset_id` column for
migration/debugging.

### `blackdog codex link`

Build an opt-in Codex desktop deep link for an active Blackdog task worktree.

```bash
blackdog codex link --project-root /path/to/task-worktree
blackdog codex link --project-root /path/to/repo --workset WORKSET --task TASK --json
```

The command resolves the active in-progress task, verifies that its durable
worktree still exists, and emits a `codex://threads/new` URL whose `path` is
that exact workspace. Opening the URL creates a **new local chat** in Codex;
it does not attach the calling thread, create a Codex-managed worktree, or
transfer workspace ownership. The prompt is prefilled but is not submitted
automatically.

Blackdog remains responsible for worktree creation, branch identity, landing,
and cleanup. The bounded continuation prompt contains only task/workset
identity and recovery instructions: it tells Codex to run `task show` and
follow the returned `next_action` exactly. It never copies the raw user prompt,
the execution prompt, their digests, or replay-artifact paths into the link.

JSON output is under `codex_link` and explicitly records
`workspace_owner = "blackdog"`, `workspace_role = "task"`,
`codex_workspace_kind = "local"`, `thread_continuity = "new_thread"`, and
`auto_submits = false`. It also includes `fallback_argv = ["codex", "app",
WORKSPACE]`; that stable CLI fallback opens the path but does not prefill the
continuation prompt. The command refuses completed, abandoned, missing, or
otherwise inactive task worktrees instead of producing a stale link.

### `blackdog codex coverage`

Compare Codex's own session/dialogue logs to Blackdog runtime attempts.

```bash
blackdog codex coverage --project-root /path/to/repo
blackdog codex coverage --project-root /path/to/repo --since 2026-05-01
blackdog codex coverage --project-root /path/to/repo --json
```

The command scans active and archived Codex sessions, maps sessions to the repo
and its git worktrees by cwd, classifies user turns, and relates turns to Blackdog attempts
by explicit turn refs, prompt hashes, stored Codex session refs, same-session
episodes, and active attempt windows. The legacy linked/unlinked counters still
mean strong launch relationships: explicit turn refs, prompt-hash matches, or a
safe single-turn session match. Related/unrelated counters include advisory
same-session and active-window evidence so older multi-turn Codex sessions can
be analyzed retroactively without rewriting runtime state. It reports:

Attempt-owned references are resolved before ordinary repo-cwd pruning. An
attempt with an exact thread, session path, and turn id may therefore add that
one referenced turn even when the source session cwd belongs to another repo.
The path must resolve inside the active Codex session roots and the parsed
thread and turn must match. A legacy reference without `turn_id` may recover
only one unique prompt-hash match from that exact referenced thread/session;
ambiguous or incomplete legacy references add zero turns. Session-only,
missing, corrupt, escaped, mismatched, and out-of-window references remain
nonfatal and are counted in `exact_reference_resolution_counts`; each attempt
row also reports `codex_capture_status`, `codex_capture_method`,
`codex_capture_missing_reason`, and `exact_reference_resolution`. Sibling turns
and unrelated sessions are never imported by this overlay.

Implementation-without-Blackdog detection is exposed here as
`implementation_like_unlinked_turns`: Codex user turns that look like
implementation work but are not strongly linked to a Blackdog attempt. This is
one of Blackdog's learning/report outputs for tightening repo skills,
task-class guard extension points, and operator training; it does not create or
modify tasks.

- Codex sessions and user-turn counts
- Blackdog attempt counts and active attempts
- linked vs unlinked turns and attempts
- related vs unrelated turns and attempts
- relationship counts such as `launch_turn`, `prompt_hash`,
  `active_attempt_window`, and `same_session`
- analysis-only turns
- unlinked implementation-like turns
- hook-stamped `turn_classification` on matching turn rows, plus deduplicated
  `turn_classification_counts` maps named `by_intent`, `by_domain`, and
  `by_risk`
- environment issue turn counts, evidence-hit counts, observed-vs-guidance
  evidence counts, and structured issue classes such as `missing_cli`,
  `missing_venv`, `missing_container_runtime`, `missing_python_module`,
  `missing_node_dependency`, `missing_credential`, `source_file_bad_format`,
  and `wrong_worktree_env`
- model/reasoning observability
- longest completed Codex turn duration and turn identifiers

Coverage output may show short prompt excerpts for operator diagnosis, but it
does not persist transcript text. Environment issue evidence is extracted from
tool output as `observed_failure` and from assistant/attempt prose as
`operator_guidance`; turn-level environment issue classes and the primary issue
class are based on observed failures only, while guidance evidence remains
available through the evidence rows and guidance-specific counts. Evidence is
stored as bounded excerpts; it is a read-model annotation and does not change
`failure_class`.

Parsed Codex sessions are cached under user-local Blackdog state and invalidated
by session-file size and mtime. The cache stores parsed metadata, bounded
excerpts, issue classes, tool counts, and token counters; it does not store full
prompt/response transcripts. When `--since`/`--until` bounds are available,
first-run parsing skips session files whose filename date cannot overlap the
requested window.

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

Rows contain prompt hashes, Codex session refs, capture status/method/missing
reason, exact-reference resolution, relationship labels, and bounded
environment issue evidence, not full prompts or responses. Attempt rows carry
task/result/git/validation metadata and inherit environment issue classes from
related Codex turns. Codex-turn rows cover all repo-matched user turns plus any
single exact attempt-referenced foreign-cwd turns,
including linked Blackdog launches, same-session follow-ups, and analysis-only
work that never entered WTAM. Every Codex-turn row also exposes a stable
`turn_classification` key containing the bounded object from a valid matching
hook stamp, or `null` when none exists. This does not replace the existing
session-derived `classification` field.

`execution_prompt_*` records the prompt Blackdog actually ran.
`user_prompt_*` records the raw user request Blackdog received.
The stable `prompt_*` alias is populated only when those two lineages are the
same; otherwise it is left empty so the split lineage stays explicit.

### `blackdog codex hook`

Record optional Codex hook observability. The current hook subcommand is:

```bash
blackdog codex hook stamp --project-root /path/to/repo
blackdog codex hook stamp --project-root /path/to/repo --event-json '{"hook_event_name":"Stop"}' --json
```

`hook stamp` reads one Codex command-hook JSON object from stdin by default, or
from `--event-json`/`--event-file`. It writes a bounded task-context stamp to
the repo control root at `codex/task-context.jsonl`. The stamp includes hook
metadata such as `session_id`, `turn_id`, `hook_event_name`, cwd, model, and
permission mode, plus the active Blackdog attempt inferred from the hook cwd
when one exists. When Codex supplies `prompt` or `message`, Blackdog also uses
that text transiently to produce a coarse heuristic `turn_classification` with
bounded intent, domain, risk, source, and confidence labels. The `--json` result
includes the resulting classification.

Prompt/message text, tool command text, matched classifier terms, and excerpts
are not stored. Blackdog records prompt and tool-command hashes when Codex
supplies those fields, then persists only those hashes and the bounded
classification labels. Missing prompt/message input produces an unknown,
low-confidence classification. Any other classification failure degrades to
the same bounded unknown object so the task-context stamp can still be recorded.

This is an additive observability layer. It does not create, claim, land, or
close tasks. `runtime.json` remains the attempt source of truth. `codex
coverage` and `codex history` consume hook stamps as the strong
`hook_context` relationship when a turn id and active attempt id match. A
classification risk of `guarded` only describes a deployment, external-write,
or destructive signal. It cannot activate, satisfy, bypass, or otherwise affect
the task-class guards that control task execution. A classification-only stamp
without an active attempt can still annotate and count a matching Codex turn,
but it does not create a `hook_context` relationship.

Repo-local hooks should call the repo-local launcher using a git-root based
path. Blackdog itself dogfoods the observability path with this tracked
`.codex/config.toml` shape:

```toml
[[hooks.UserPromptSubmit]]

[[hooks.UserPromptSubmit.hooks]]
type = "command"
command = '"$(git rev-parse --show-toplevel)/.VE/bin/blackdog" codex hook stamp --project-root "$(git rev-parse --show-toplevel)"'
timeout = 5

[[hooks.Stop]]

[[hooks.Stop.hooks]]
type = "command"
command = '"$(git rev-parse --show-toplevel)/.VE/bin/blackdog" codex hook stamp --project-root "$(git rev-parse --show-toplevel)"'
timeout = 5
```

Do not add `--json` to lifecycle hook commands: a successful stamp is silent,
which is valid for `Stop`, while `--json` is reserved for direct CLI callers.
The short timeout bounds observability latency. Stamp failures remain hook
failures and do not produce a guard decision, prompt block, or continuation.

Blackdog does not install these hooks automatically during `repo install`.
Project-local hooks require Codex hook trust review and should be opted into by
the repo/operator; the checked-in Blackdog configuration is that explicit
repo-level opt-in.

## Removed Or Deferred Commands

The old backlog-centric commands are not part of the shipped surface.
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
