# CLI

Blackdog exposes a fixed command protocol for repository tasks. Commands write
human-readable text by default where useful and stable JSON when `--json` is
available. Agents should prefer JSON for lifecycle control.

## Invocation Rules

- Use the installed standalone executable; Blackdog source development uses
  `./scripts/blackdog`. Use the exact workspace executable in the task receipt.
- `--project-root` defaults to the current directory unless documented
  otherwise.
- Never edit `runtime.json`, `events.jsonl`, prompt artifacts, or transaction
  evidence directly.
- `task begin` is the sole normal implementation entrypoint.
- `worktree preflight` and `worktree table` are read-only.
- The target branch recorded on the attempt is authoritative.

## Task Result Contract

Structured results from task lifecycle operations include:

- `operation`
- `operation_status`
- `task_status` and `attempt_status`
- `disposition`
- `mutation_started`, `mutation_completed`, and `mutation_phase`
- `failure_code`
- `next_action`

`next_action` is the only execution authority. It contains:

- `action_id`, `kind`, `disposition`, and bounded reason fields
- `display`
- `argv` and its `command` rendering for `kind=command`
- `choices` for `kind=choice`
- `alternatives` allowed by a command result
- `required_inputs` for `kind=blocked`
- `safety_class` and `mutation_class`

Allowed kinds:

| Kind | Meaning |
|---|---|
| `command` | Execute the exact nonempty `argv`. |
| `choice` | Select one complete emitted choice. |
| `complete` | Stop; no command is required. |
| `blocked` | Stop and satisfy the named inputs or external condition. |

Do not execute `display`, reason text, error prose, or a reconstructed command.
Machine-emitted retry arguments bind an exact request and state generation;
never omit, edit, or reuse them for another task.

## Task Commands

### `blackdog task begin`

Create a task and start its first attempt, or resume an existing task through
an exact machine-emitted command.

```bash
blackdog task begin \
  --project-root . \
  --actor codex \
  --execution-prompt-file /private/path/execution.txt \
  --request-file /private/path/request.txt \
  --prompt-mode skill \
  --json
```

User-facing inputs:

- `--project-root`
- `--actor` (default `codex`)
- Exactly one of `--execution-prompt` or `--execution-prompt-file`
- Optional exactly one of `--request` or `--request-file`
- `--prompt-mode raw|skill`
- Optional `--title`, `--branch`, `--from`, `--path`, `--model`,
  `--reasoning-effort`, and `--note`
- Optional `--show-prompt` and `--json`

When request input is omitted, the execution input supplies both lineage roles.
File inputs are preferable because they are replayable without shell quoting.
Prompt text is normalized, hashed, and persisted privately before it is stored
by reference on the attempt.

The command checks repository and managed-skill readiness, guards, Git base and
target identity, handler readiness, and prompt lineage. It then creates the task
branch/worktree, executes configured handlers there, and atomically starts the
attempt. A retained partial operation returns its exact repair action.

`--task` and expected-identity arguments are hidden recovery capabilities. They
appear only in emitted retry commands and are not ordinary task-authoring flags.

### `blackdog task show`

```bash
blackdog task show --project-root . --json
blackdog task show --project-root . --task TASK_ID --json
```

Resolve from the current task worktree or an explicit task ID and report:

- Current and latest attempt state
- Branch, target, workspace, dirty, and commit evidence
- Prompt and provider-reference lineage
- Landing, close, cleanup, and reconciliation progress
- Structured recovery issues
- Authoritative `next_action`

The operation is read-only.

### `blackdog task recover`

```bash
blackdog task recover --project-root . --json
blackdog task recover --project-root . --task TASK_ID --json
```

Inspect durable state, prompt artifacts, event evidence, Git references,
worktree ownership, and incomplete product transactions. Mutating repair flags
are machine-emitted and identity-guarded. Ordinary callers should first run the
read-only command and follow its `next_action`.

### `blackdog task cancel`

```bash
blackdog task cancel \
  --project-root . \
  --task TASK_ID \
  --actor codex \
  --summary "No longer required" \
  --json
```

Cancel an inactive task. Required inputs are `--task` and `--actor`. Optional
structured failure inputs are `--failure-class`, `--recovery-action`,
`--prompt-issue`, and `--operator-issue`. An active attempt must be closed; it
cannot be canceled by takeover.

### `blackdog task reopen`

```bash
blackdog task reopen \
  --project-root . \
  --task TASK_ID \
  --actor codex \
  --summary "Required again" \
  --json
```

Return a canceled task to a restartable state. Reopen does not start an
attempt; follow its returned action.

### `blackdog task land`

```bash
blackdog task land \
  --project-root . \
  --summary "Remove obsolete state layers" \
  --validation unit-tests=passed \
  --validation public-check=passed \
  --json
```

Resolve the active task from the current worktree or optional `--task`. The
first request requires:

- A nonblank `--summary`
- At least one repeated `--validation NAME=passed|failed|skipped`

It may also include repeated `--residual` and `--followup`, optional `--note`,
and `--keep-worktree`.

Landing records an immutable request and advances its phased transaction. It
creates the canonical commit, compare-and-swaps the recorded target, finalizes
runtime and event evidence, then removes the source workspace unless retention
was requested. A retry reuses the recorded request and validates completed
phases before advancing.

Automatic stale recovery runs only when enabled by repository policy. Any
conflict, unsafe state, validation failure, or exhausted retry preserves the
task workspace and returns a typed action.

### `blackdog task reconcile-landing`

```bash
blackdog task reconcile-landing \
  --project-root . \
  --task TASK_ID \
  --attempt ATTEMPT_ID \
  --landed-commit COMMIT \
  --actor codex \
  --json
```

Prove a current-format canonical commit when Git evidence is complete but
terminal runtime evidence is missing. Required identity is never inferred.
Without `--apply`, the command is read-only. Apply only when an exact prior
result emits an action containing `--apply`.

The proof requires target reachability, canonical task/attempt/actor/status
trailers, changed-path equality, and source equivalence when source evidence is
available. Unsupported commit formats fail closed.

### `blackdog task close`

```bash
blackdog task close \
  --project-root . \
  --status blocked \
  --summary "Requires an external decision" \
  --validation unit-tests=skipped \
  --json
```

Close the current attempt without landing. Status is
`blocked|failed|abandoned`. The first request supplies a nonblank summary and
real validation evidence; residuals, follow-ups, note, failure fields, and
`--cleanup` are optional.

The close request is immutable after it is recorded. Cleanup occurs only after
core finalization and only with exact ownership and disposal proof. A retained
source is an explicit outcome, not a hidden failure.

### `blackdog task cleanup`

```bash
blackdog task cleanup --project-root . --json
blackdog task cleanup --project-root . --task TASK_ID --json
```

Remove a retained task worktree and its task branch after proving ownership and
disposability. Optional `--path` and `--branch` narrow recovery after repository
metadata loss. Ambiguous or unlanded sources block. A missing exact workspace
or branch is an idempotent no-op.

## Worktree Diagnosis

### `blackdog worktree preflight`

```bash
blackdog worktree preflight --project-root . --json
```

Read-only repository readiness: primary checkout, Git state, managed runtime,
handlers, and task-worktree location. This is diagnosis, not a prerequisite for
`task begin`.

### `blackdog worktree table`

```bash
blackdog worktree table --project-root . --json
```

Show one row per active attempt or terminal attempt whose recorded worktree
still exists. Stable fields cover task and attempt identity/status, actor,
branch and target branch, recorded path and existence/dirty state, branch
existence/divergence, and landed commit. Mutation is available only through
task commands.

## Task and Attempt Reporting

### `blackdog summary`

```bash
blackdog summary --project-root .
blackdog summary --project-root . --json
```

Show canonical task counts, tasks, and recent attempts, including terminal and
canceled task history.

### `blackdog snapshot`

```bash
blackdog snapshot --project-root .
```

Emit the complete machine-readable derived runtime snapshot as JSON. Snapshot
data is not another state authority.

### `blackdog attempts summary|table`

```bash
blackdog attempts summary --project-root . --json
blackdog attempts table --project-root . --json
```

Report completed attempt history. `summary` aggregates status and elapsed-time
evidence. `table` emits stable columns for automation.

### `blackdog stats`

```bash
blackdog stats --project-root . --since 2026-08-01 --json
blackdog stats --root /path/to/repos --timezone UTC --tsv
blackdog stats --registry
```

Read one or more explicit projects, discovery roots, or the local registry.
Time flags are `--since`, `--until`, `--by day`, and `--timezone`. Stats derives
task, attempt, provider-turn, prompt, validation, environment, and bounded
observability counts without mutating task state.

## Prompt Inspection

### `blackdog prompt preview`

```bash
blackdog prompt preview --project-root . --request-file /private/path/request.txt
blackdog prompt preview --project-root . --request "Refactor parser" --show-prompt --json
```

Exactly one of `--request` or `--request-file` is required. Preview reports the
deterministic repository-contract composition without starting a task.
`--expand-skill-text` and `--expand-contract` explicitly include routed text;
the default includes bounded references only.

The command does not optimize, rewrite, or promote prompts.

## Provider Evidence

### `blackdog codex coverage`

```bash
blackdog codex coverage --project-root . --since 2026-08-01 --json
```

Compare provider session turns with Blackdog attempts and report linked,
unlinked, ambiguous, and missing evidence. It does not copy conversations.

### `blackdog codex history`

```bash
blackdog codex history --project-root . --since 2026-08-01
blackdog codex history --project-root . --jsonl --write
```

Render compact turn/attempt history. `--write` refreshes the derived private
history artifact; it does not change canonical task state.

### `blackdog codex hook stamp`

```bash
blackdog codex hook stamp --project-root . --event-file /private/path/event.json --json
```

Accept at most one of `--event-json` and `--event-file`, resolve active task
context, and append bounded hook evidence. Hook input cannot create or complete
a task.

## Repository Lifecycle

### `blackdog init`

```bash
blackdog init --project-root . --project-name example
```

Write a default `blackdog.toml` for an initialized repository.

### `blackdog repo analyze`

Read-only inspection of a target repository and proposed Blackdog setup.

### `blackdog repo bind`

Bind an existing repository to a managed source checkout and generated contract
surfaces.

### `blackdog repo install`

Install or repair the repo-local launcher, handlers, managed skill, and contract
surface. Optional inputs are `--project-name` and `--source-root`.

### `blackdog repo update`

Refresh the managed launcher and source path. It does not rewrite repository
policy.

### `blackdog repo refresh`

Regenerate managed instructions and skill content from the installed contract.

### `blackdog repo scaffold`

```bash
blackdog repo scaffold \
  --target-root /path/to/new-repo \
  --project-name example \
  --like /path/to/exemplar \
  --dry-run
```

Create or preview a new repository using the current contract. `--target-root`
is required; `--like`, `--source-root`, and `--dry-run` are optional.

### `blackdog repo table`

```bash
blackdog repo table --project-root . --json
blackdog repo table --root /path/to/repos --since-hours 24
blackdog repo table --registry --include-archived --no-codex
```

Report membership, version, runtime format, handler state, task/attempt counts,
and optional provider evidence for explicit scope.

### `blackdog repo archive|unarchive`

Mark local membership inactive or active. Archive accepts optional `--reason`;
neither command deletes task evidence.

### `blackdog repo unbind`

Preview managed-file removal by default. `--confirm` applies it;
`--keep-control-dir` preserves private control evidence.

## Local Registry

```bash
blackdog local-repo add --project-root . --json
blackdog local-repo list --json
blackdog local-repo remove --project-root . --json
```

The user-local registry is an explicit reporting convenience. It is not task
truth and does not discover repositories automatically.

## Removed and Deferred Surfaces

The CLI intentionally has no hidden task authoring, automatic subsequent-task
selection, prompt rewriting, provider chat launcher, or low-level worktree
mutation alias. Do not reconstruct these flows from internal functions.

Direct task relationships remain deferred. Runtime independence and standalone
Python release packaging are shipped; see [runtime distribution](RUNTIME_DISTRIBUTION.md).

## Store upgrades

When an installed runtime rejects an older store, use the explicit migration
preview. The CLI emits this concrete action for old or pending stores:

```bash
blackdog repo migrate --project-root /path/to/repo --json
```

Review the task/attempt counts and archive location, then execute the returned
`next_action.argv`. Apply requires `--apply` and the preview's exact
`--expected-digest`; never invent that digest. Re-run the same action after an
interruption. Source drift or unfinished legacy work stops without discarding
history. This release migrates terminal schema-3 stores with matching planning
schema 1; active claims, retained workspaces, and other formats require resolution
before cutover. Normal `summary` and task creation work after migration.

`repo update` checks the existing store before changing launchers or managed
sources. It returns the migration action on a version mismatch instead of
installing an unusable launcher. This cannot prevent an externally shared source
checkout from advancing independently; the same migration route repairs that case.

### Typed outcome and machine validation evidence

`blackdog task outcome --task TASK_ID --json` reads typed outcome detail.
`--attempt ATTEMPT_ID` plus exactly one of `--definition-file`,
`--assessment-file`, or `--measurement-file` records bounded JSON evidence.
`--actor` defaults to `codex`. Outcome and validation commands always return
JSON. They record evidence and do not change task lifecycle state.

`blackdog task validate --task TASK_ID --attempt ATTEMPT_ID --run-id RUN_ID
--json` runs configured commands with an immutable invocation receipt. A
completed retry does not rerun commands; interrupted invocations without
results remain indeterminate. Failed, stale, unknown or indeterminate results
exit nonzero. Receipt applicability does not authorize landing.

`blackdog stats --no-codex --json` reads runtime and typed outcome evidence
without provider history. Its compact outcome cohorts expose comparable
identities, sample denominators and missingness; per-task detail is available
through `task outcome`.

See [Outcome evidence](OUTCOME_EVIDENCE.md) for complete input schemas,
provenance boundaries, correction semantics and timing interpretation.
