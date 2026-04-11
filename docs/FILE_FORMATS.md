# File Formats

Mutable runtime state now lives under one shared local control root across worktrees. By default, `paths.control_dir = "@git-common/blackdog"` resolves to `<git-common-dir>/blackdog`, which is shared by the primary checkout and all linked worktrees.

The near-term contract is intentionally one format. The default backlog lives at `<control_dir>/...`, and any named backlog lives at `<control_dir>/backlogs/<slug>/...` using the exact same file set. Blackdog does not use a separate test-only schema.

By default, the rendered HTML board is repo-branded:

- default backlog: `<control_dir>/<project-slug>-backlog.html`
- named backlog: `<control_dir>/backlogs/<slug>/<project-slug>-<slug>-backlog.html`
- compatibility alias: `backlog-index.html` beside the rendered HTML file

## Core charter for file contracts

This document covers both the frozen `blackdog_core` contract and
Blackdog-product artifacts layered on top of it.

Executable and module packaging surfaces are intentionally out of scope
here: freeze those in `pyproject.toml`,
[docs/CLI.md](docs/CLI.md), and [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md).
This document only freezes durable runtime artifacts and their write
semantics.

Treat these as the required core contract for extraction and hardening
work:

- `blackdog.toml`
- `<control_dir>/backlog.md`
- `<control_dir>/backlog-state.json`
- `<control_dir>/events.jsonl`
- `<control_dir>/inbox.jsonl`
- `<control_dir>/task-results/`
- `blackdog worktree preflight --format json`

Treat these as Blackdog-product surfaces that must not be used to
expand `blackdog_core` by default:

- `<control_dir>/threads/`
- `<control_dir>/supervisor-runs/`
- `<control_dir>/tracked-installs.json`
- `.codex/skills/<skill-name>/.blackdog-managed.json`
- the rendered HTML page and `blackdog snapshot` payload

Current file placement is transitional; ownership follows this charter,
not whichever module or command currently writes a file.

## Vocabulary

Use these terms consistently in code and docs:

- `State`: current mutable authority stored in `backlog-state.json`;
  today that means approval records and task-claim records only
- `Record`: one durable append-only artifact such as an event record,
  inbox record, or task-result payload
- `Plan`: execution intent stored in the backlog plan block
- `Snapshot`: a derived read-only projection assembled from backlog,
  state, and records
- `Row`: a storage or view row when the physical format is explicitly
  row-oriented, such as one JSONL line or one rendered table/list row

## `blackdog.toml`

Repo-local profile file.

Required sections:

- `[project]`
- `[paths]`
- `[ids]`
- `[rules]`
- `[supervisor]`
- `[taxonomy]`
- `[pm_heuristics]`

Current keys:

- `[project]`
  - `name`
  - `profile_version`
- `[paths]`
  - `control_dir`
  - `skill_dir`
  - `worktrees_dir`
  - optional explicit overrides:
    - `backlog_dir`
    - `backlog_file`
    - `state_file`
    - `events_file`
    - `results_dir`
    - `threads_dir`
    - `inbox_file`
    - `html_file`
    - `supervisor_runs_dir`
- `[ids]`
  - `prefix`
  - `digest_length`
- `[rules]`
  - `require_claim_for_completion`
  - `auto_render_html`
- `[supervisor]`
  - `launch_command`
  - `max_parallel`
- `[taxonomy]`
  - `buckets`
  - `domains`
  - `validation_commands`
  - `doc_routing_defaults`
- `[pm_heuristics]`
  - free-form string settings used for repo-local guidance

Current supervisor launcher contract:

- `launch_command` is the argv prefix used for each child launch.
- The default value is `["codex", "exec", "--dangerously-bypass-approvals-and-sandbox"]`.
- Blackdog prefers the desktop Codex.app runtime when the default launcher is configured and that binary is installed.
- Prompt-style Codex launchers are no longer supported by the supervisor.
- Supervisor child runs do not have a wall-clock task timeout; they stay claimed until they finish the protocol or the supervisor recovers an orphaned claimed pid after repeated failed liveness scans.
- `control_dir` accepts the special prefix `@git-common`, which resolves against `git rev-parse --git-common-dir`.
- The default `worktrees_dir` value is `../.worktrees`, which places branch-backed task worktrees beside the primary checkout by default.
- Child workspaces are branch-backed task worktrees created from the primary worktree branch.
- Dirty primary-worktree state still blocks branch landing as a contract violation, and the child run records that blocked outcome first. Before any later child launch from the same idle supervisor loop, Blackdog re-evaluates the primary checkout and either lands the blocked branch after cleanup, commits a matching dirty primary checkout, or stashes unrelated dirty state into a follow-up backlog task.
- Successful branch-backed child runs are landed through the primary worktree with fast-forward semantics, and the supervisor completes the task after a successful land.
- Child run snapshots now distinguish completion outcome with:
  - `latest_run_branch_ahead`: whether the branch was ahead of the target when the run ended.
  - `latest_run_landed`: whether a landing commit was produced.
  - `latest_run_land_error`: landing failure text when the run is blocked.

## `<control_dir>/backlogs/<slug>/...`

Named backlog root.

Each named backlog reuses the same artifact layout as the default backlog root:

- `backlog.md`
- `backlog-state.json`
- `events.jsonl`
- `inbox.jsonl`
- `task-results/`
- `threads/`
- `<project-slug>-<slug>-backlog.html`
- `backlog-index.html` (compatibility alias)
- `supervisor-runs/`

These named roots are created and removed with `blackdog backlog new NAME` and `blackdog backlog remove NAME`. The default CLI still operates on the default backlog unless a command explicitly targets a named root in the future.

## `coverage/latest.json` (or configured `tool.blackdog.coverage.artifact_output`)

`blackdog coverage` writes a JSON report when `--output` is set or when
`[tool.blackdog.coverage].artifact_output` exists in `pyproject.toml`.

The emitted schema includes:

- `project_root`
- `profile`
- `status`
- `runs`: command execution results, each with:
  - `command`
  - `status`
  - `returncode`
  - `elapsed_seconds`
  - `stdout`
  - `stderr`
  - `coverage`
- `summary`: aggregated coverage totals for modules under `src/`, or the focused
  shipped surface when a coverage run uses `--command` with
  `[tool.blackdog.coverage].shipped_surface` configured.

`runs[*].coverage` maps `module_path` → `{covered, total, coverage_percent}`.
`summary` also includes:

- `modules`
- `module_count`
- `total_lines`
- `covered_lines`
- `coverage_percent`

`[tool.blackdog.coverage].shipped_surface` defines the current focused audit
surface for core semantics. In this repo that surface is now:

- `src/blackdog_core/backlog.py`
- `src/blackdog_core/profile.py`
- `src/blackdog_core/snapshot.py`
- `src/blackdog_core/state.py`

Use `make coverage-core` to capture a focused coverage artifact at
`coverage/core-latest.json` for that surface. `make coverage` remains the broad
repo-level validation pass. While deriving those totals, Blackdog ignores
stdlib trace `>>>>>>` markers for non-executable multiline signature
continuation lines and still counts real missing trace lines as uncovered.

Current gaps to close before a true 100 percent core gate is defensible:

- `blackdog_core.backlog.py`: add direct tests for runnable-state classification,
  predecessor/approval transitions, and stale-state pruning in addition to
  task-shaping coercion.
- `blackdog_core.profile.py`: add direct tests for profile parsing failures, `@git-common`
  path resolution, and default doc-routing/validation propagation.
- `blackdog_core.state.py`: add direct tests for malformed JSON and JSONL rejection,
  append-only inbox replay, and result ordering invariants.
- `blackdog_core.snapshot.py`: add direct tests for runtime summary and
  runtime snapshot builders.

Those focused tests should be the core gate. The existing CLI, supervisor, and
render tests should remain integration coverage rather than the primary source
of confidence in core semantics.

## Core durable state tables, state machines, and invariants

Phase 0 of the core hardening audit freezes the durable write-path contract to
five artifacts:

| Artifact | Core-owned state | Write model | Runtime view |
| --- | --- | --- | --- |
| `backlog-state.json` | `approval_tasks` | latest JSON object snapshot | approval state keyed by task id |
| `backlog-state.json` | `task_claims` | latest JSON object snapshot | claim/completion state keyed by task id |
| `backlog-state.json` | `task_attempts` | latest JSON object snapshot | attempt lineage keyed by `attempt_id` |
| `backlog-state.json` | `wait_conditions` | latest JSON object snapshot | durable wait state keyed by `wait_id` |
| `events.jsonl` | event records | append-only JSONL replay | factual history, never in-place mutation |
| `inbox.jsonl` | inbox records | append-only JSONL replay | effective inbox state keyed by `message_id` |
| `task-results/<task-id>/*.json` | result records | append-only per-task files | newest-first derived result history |

The current runtime semantics for those artifacts are:

- `approval_tasks` only exists for tasks whose backlog payload sets
  `requires_approval = true`. `sync_state_for_backlog()` seeds missing entries
  as `pending`, refreshes task metadata on every backlog load, promotes already
  completed tasks to `done`, and removes the entry when the task no longer
  requires approval or the task no longer exists in the backlog snapshot.
- `task_claims` is the authoritative source for both active claims and durable
  completion. `task_done()` only looks at `task_claims[task_id].status ==
  "done"`, and `claim_is_active()` only treats `status == "claimed"` as an
  active owner. Reconcile passes refresh stored task metadata from the backlog
  and prune orphaned claim rows for task ids that no longer exist.
- `task_attempts` is the additive execution-lineage table. It records
  per-attempt runtime metadata such as `run_id`, workspace binding, prompt
  receipt, landing outcome, and latest attempt status keyed by `attempt_id`.
- `wait_conditions` is the additive durable wait table. It records what an
  attempt is waiting on, the current wait state, and the resume/detail text
  Blackdog can surface through status or snapshot views.
- `events.jsonl` is append-only. Readers must derive current state from replay;
  no event row is updated or deleted after it is written. Every row must remain
  a JSON object with `event_id`, `type`, `at`, `actor`, and an object-valued
  `payload`. Event rows may also carry correlation fields such as `task_id`,
  `attempt_id`, `run_id`, `wait_condition_id`, and `control_message_id`.
- `inbox.jsonl` is append-only. `load_inbox()` rebuilds message state by
  replaying rows in file order and folding them by `message_id`, after first
  validating row shape (`action`, `message_id`, `at`, and the action-specific
  required fields). Message rows may additionally carry typed control metadata
  such as `control_action`, `control_scope`, `control_target`, `control_state`,
  and `control_reason`.
- `task-results/<task-id>/*.json` is append-only evidence. `record_task_result()`
  always writes a new timestamped file and appends a matching `task_result`
  event; `load_task_results()` derives presentation order by sorting rows by
  `recorded_at` descending and rejects files that do not carry the required
  summary fields. Result rows may additionally carry `attempt_id`,
  `wait_condition_id`, `control_message_id`, and an embedded `prompt_receipt`.

`blackdog_core.snapshot.load_runtime_artifacts()` is the canonical read
path over those five artifacts. It:

- runs the same state reconciliation logic every core command already depends
  on;
- returns deterministic reconcile counters such as pruned claim/approval rows
  and promoted done approvals; and
- powers `blackdog validate`, which now performs strict artifact checks over the
  live inbox/result surfaces instead of only returning lightweight counters.

### `approval_tasks` semantic state machine

Blackdog does not currently enforce a strict transition graph between every
approval decision. The frozen semantic machine is:

- `absent` means the task does not currently require approval or has never been
  synchronized into state.
- `pending` is the seeded default for a `requires_approval` task.
- `approved`, `denied`, `deferred`, and `done` are the accepted stored decision
  values written by `blackdog decide`.
- Runnable checks only treat `approved` and `done` as approval-satisfied.
- `blackdog complete` promotes any existing approval entry to `done`.
- Clearing `requires_approval` or removing the task returns the entry to
  `absent`.

### `task_claims` semantic state machine

The normal-flow claim machine is:

- `absent` means there is no stored claim row for the task.
- `claimed` is written by `blackdog claim` and `blackdog task run`.
- `released` is written by `blackdog release` and clears pid-tracking fields.
- `done` is written by `blackdog complete`, clears pid-tracking fields, and is
  the only status that removes the task from runnable scheduling.

Operational invariants:

- Only `claimed` counts as an active owner.
- `released` and `absent` are equivalent for runnable selection.
- `done` is terminal for scheduler semantics even though forced administrative
  rewrites can still mutate state outside the normal path.
- `claimed_pid`, `claimed_process_missing_scans`,
  `claimed_process_last_seen_at`, and `claimed_process_last_checked_at` are only
  meaningful while the stored status is `claimed`; reconcile/normalization drops
  those fields for any other stored status.

### `inbox.jsonl` replay state machine

The inbox replay contract is:

- `action = "message"` creates or replaces the current open message record for
  that `message_id`.
- `action = "resolve"` marks a previously seen message as resolved.
- Resolve rows for unknown `message_id` values are ignored.
- If multiple resolve rows exist for the same message, the last replayed row
  wins for `resolved_at`, `resolved_by`, and `resolution_note`.
- Replay first validates that message rows carry `sender`, `recipient`, `kind`,
  `body`, and list-valued `tags`, and that resolve rows carry `actor`.

The corresponding code-level state sets are:

- `blackdog_core.state.APPROVAL_STATE_MACHINE_STATES`
- `blackdog_core.state.CLAIM_STATE_MACHINE_STATES`
- `blackdog_core.state.INBOX_ACTIONS`
- `blackdog_core.state.INBOX_STATE_MACHINE_STATES`

### `task-results/<task-id>/*.json` append-only invariants

- Result rows are immutable once written.
- A result row must carry the required summary fields documented above, even
  when `metadata` or `task_shaping_telemetry` is empty.
- The `status` field is stored as the caller supplies it; current child/manual
  workflows conventionally use `success`, `blocked`, or `partial`, but the file
  contract is append-only evidence rather than an enum-enforced state machine.
- Readers validate the result shape on load, including list-valued summary
  fields plus object-valued `metadata` and `task_shaping_telemetry`.
- Strict runtime validation also requires every result file to retain a matching
  `task_result` event carrying the same `task_id`, `run_id`, and `result_file`.

## `blackdog worktree preflight --format json` WTAM invariants

The worktree contract JSON is a readonly invariant view over the current
workspace. In addition to branch/worktree cleanliness facts, the payload now
includes:

- `current_task_id`: the backlog task id implied by the current branch name when
  the branch matches a known task branch.
- `current_branch_is_task_branch`: whether the current branch name resolves to a
  known backlog task id.
- `workspace_role`: one of `primary`, `task`, or `linked`.
- `landing_state`: `ready` when the primary worktree is clean for landing, or
  `blocked` when dirty primary-worktree state would prevent landing.

### `blackdog worktree` lifecycle and landing state machine

`blackdog` freezes worktree lifecycle states to
`blackdog.worktree.WORKTREE_LIFECYCLE_STATES`:

- `prepared`: `worktree start` created the branch-backed task workspace.
- `dirty`: the current worktree has local changes.
- `ahead`: the task branch contains commits ahead of the target branch.
- `blocked`: landing is prevented by dirty primary state or another worktree
  cleanliness violation.
- `landed`: `worktree land` fast-forwarded the task branch into the target.
- `cleaned`: `worktree cleanup` removed the task worktree and optionally deleted
  the branch.

The readonly worktree contract also freezes two derived states:

- `workspace_role = primary|task|linked`
- `landing_state = ready|blocked`

## Core coverage gate plan

The core-only audit surface is frozen to `[tool.blackdog.coverage].shipped_surface`:

- `src/blackdog_core/backlog.py`
- `src/blackdog_core/profile.py`
- `src/blackdog_core/snapshot.py`
- `src/blackdog_core/state.py`

The focused audit command is frozen to the current `Makefile` contract:

- `make test-core` runs `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_core_*.py'`
- `make coverage-core` runs the same focused audit through `blackdog coverage`
  and writes `coverage/core-latest.json`

The gate plan is intentionally two-phase:

- Phase 0 gate: keep the shipped surface, focused audit command, and coverage
  artifact path frozen; require `make test-core` and `make coverage-core` to
  pass, but do not enforce a numeric coverage threshold yet. The current
  pass/fail signal is command success plus the frozen artifact write, not a
  minimum coverage percentage.
- Phase 1 hard gate: once the direct tests listed in the coverage gap audit
  above land, require 100.0 percent aggregate coverage across the shipped
  surface and 100.0 percent coverage for each shipped module before additional
  core extraction or ownership moves.

Phase 0 stays evidence-only until Blackdog explicitly turns on the Phase 1
numeric gate. A current `make coverage-core` run may report less than 100.0
percent or even happen to hit 100.0 percent, but the shipped-surface pass/fail
contract still comes from command success plus artifact retention rather than a
coverage threshold.

## `<control_dir>/threads/<thread-id>/...`

Conversation-thread runtime artifacts.

These files are Blackdog-product collaboration artifacts layered on top
of the core backlog/state contract.

Each thread directory contains:

- `thread.json`
- `entries.jsonl`

`thread.json` schema:

- `schema_version`
- `thread_id`
- `title`
- `status`
- `created_at`
- `created_by`
- `task_ids`

`entries.jsonl` rows:

- `schema_version`
- `entry_id`
- `thread_id`
- `role` (`user`, `assistant`, or `system`)
- `kind`
- `actor`
- `body`
- `created_at`
- optional `duration_seconds`
- optional `task_id`
- `metadata`

These files hold the freeform markdown conversation that Emacs uses for prompt authoring, prompt preview, and task launch.

## `<control_dir>/backlog.md`

Human-readable backlog plus machine-readable fenced JSON blocks.

Machine blocks:

- Exactly zero or one `json backlog-plan` block
- Zero or more `json backlog-task` blocks

Each task block requires:

- `id`
- `title`
- `bucket`
- `priority`
- `risk`
- `effort`
- `paths`
- `checks`
- `docs`
- `requires_approval`
- `approval_reason`
- `safe_first_slice`
- `task_shaping`

## Planning model

- `task`: the executable unit. Claims, results, completion, and dependencies are tracked at task level.
- `epic`: a thematic grouping for related tasks. Epics organize reporting and intent; they do not control runnable order.
- `lane`: a temporary ordered slot inside the execution map. Lane order is preserved top-to-bottom in the plan and UI, and the current scheduler advances lane tasks in that order.
- `wave`: a temporary concurrency boundary that opens a group of lanes together. Blackdog only considers the lowest unfinished wave runnable, so wave `1` waits for unfinished work in wave `0`, then compacts active waves back to small integers between runs.

Clarifications:

- lanes and waves are planning/scheduling structures, not executable objects
- only tasks are claimable, completable, and result-bearing
- lanes capture execution-map order inside a concurrent work area
- waves capture which set of lanes is currently open for concurrent progress
- a wave is a scheduler gate, not a dependency node; task-to-task predecessor relationships still drive runnable checks inside an open wave
- completed tasks stay in the current execution map for the rest of that active supervisor run, then disappear on the next run's opening sweep if they are still done

In practice: `epic` answers "why this cluster exists", `lane` answers "which ordered slot the task is currently in", `wave` answers "which concurrent lane group is currently open", and `task` is the unit an agent actually executes.

## `.codex/skills/<skill-name>/.blackdog-managed.json`

Project-local managed-skill manifest.

Written by `blackdog bootstrap`, `blackdog refresh`, and `blackdog update-repo`.

Schema:

- `schema_version`
- `files`
  - `<relative path>` → `{ "sha256": "<generated-content-hash>" }`

Blackdog uses this manifest to tell whether a managed skill file still matches the last generated version. If the file diverged locally, refresh leaves it in place and writes a `*.blackdog-new` sidecar beside it instead of overwriting it.

## `<control_dir>/tracked-installs.json`

Machine-local registry of Blackdog repos tracked from one development checkout.

This file is not part of a host repo's shared contract. It is owned by
`blackdog`, not `blackdog_core`. It lives under the current checkout's
shared control root so one local Blackdog development repo can
remember which host repos it manages on that machine.

Schema:

- `schema_version`
- `repos`
  - `project_root`
  - `project_name`
  - `profile_file`
  - `control_dir`
  - `blackdog_cli`
  - `added_at`
  - `last_update`
    - `at`
    - `status`
    - `blackdog_source`
    - optional `error`
  - `last_observation`
    - `at`
    - `counts`
    - `next_rows`
    - optional `host_integration_findings`
      - `category`
      - `severity`
      - `finding`
    - `tune_focus`
    - `tune_summary`

## `<control_dir>/backlog-state.json`

Structured execution state.

This file is intentionally narrow: events, inbox messages, task-result
payloads, conversation threads, tracked installs, and supervisor runs
are not part of state. They are separate append-only records or
product-owned artifacts.

Top-level keys:

- `schema_version`
- `approval_tasks`
- `task_claims`

Claim entries are authoritative when `status = "claimed"`; they no longer expire by lease time. Optional process-tracking fields may be present:

- `claimed_pid`
- `claimed_process_missing_scans`
- `claimed_process_last_seen_at`
- `claimed_process_last_checked_at`

Legacy `claim_expires_at` values may still exist in older state files, but the runtime no longer uses them to expire claims.

## `<control_dir>/events.jsonl`

Append-only event log.

Writers serialize updates with a sibling lock file and rewrite the
file via atomic replace, so readers observe the last complete JSONL
snapshot instead of partial appended rows.

Canonical event types include:

- `init`
- `task_added`
- `task_updated`
- `task_removed`
- `claim`
- `release`
- `complete`
- `decision`
- `comment`
- `render`
- `message`
- `message_resolved`
- `thread_created`
- `thread_entry_added`
- `thread_task_linked`
- `task_result`
- `worktree_start`
- `worktree_land`
- `worktree_cleanup`
- `supervisor_run_started`
- `supervisor_run_sweep`
- `child_launch`
- `child_launch_failed`
- `child_finish`
- `supervisor_run_finished`

`child_finish` event payloads carry landing outcome fields:

- `run_id`
- `child_agent`
- `exit_code`
- `missing_process`
- `result_recorded`
- `final_task_status`
- `land_error`
- `branch_ahead`
- `landed`
- `landed_commit`
- `launch_command`
- `launch_command_strategy`
- `launch_settings`
- `prompt_template_version`
- `prompt_template_hash`
- `prompt_hash`

`launch_command` is the resolved argv prefix and includes launcher selection details (for example, replacing default `codex` with desktop `Codex.app`), `launch_settings` captures the resolved launcher/model/reasoning view for that attempt, and `prompt_*` fields capture the child prompt fingerprint used for that run.

When Blackdog can build launch telemetry before a child process starts, `child_launch_failed` payloads should also carry `launch_command`, `launch_command_strategy`, and `launch_settings` so report surfaces can explain launch-policy failures instead of only the error text.

`worktree_land` event payloads carry the landed branch outcome for direct/manual WTAM flow:

- `branch`
- `target_branch`
- `primary_worktree`
- `target_worktree`
- `landed_commit`
- `diff_file`
- `diffstat_file`

When the landing belongs to a Blackdog task, `task_id` should be set on the event so completed-task views can render landed status without relying on supervisor-only `child_finish` records.

## `<control_dir>/inbox.jsonl`

Append-only message channel.

Writers serialize updates with a sibling lock file and rewrite the
file via atomic replace while preserving append-order row semantics.

Rows use:

- `action = "message"`
- `action = "resolve"`

The effective inbox state is derived by replaying rows by `message_id`.

## `<control_dir>/task-results/<task-id>/*.json`

Structured run artifacts for child agents or manual task completions.

Required keys:

- `task_id`
- `recorded_at`
- `actor`
- `status`
- `what_changed`
- `validation`
- `residual`
- `needs_user_input`
- `followup_candidates`
- `task_shaping_telemetry`
- `metadata`

`task_shaping_telemetry` stores measured or auto-derived task-shaping fields for this result row.
Current auto-filled keys may include:

- estimate snapshot fields such as `estimated_active_minutes`,
  `estimated_elapsed_minutes`, `estimated_touched_paths`,
  `estimated_validation_minutes`, `estimated_worktrees`,
  `estimated_handoffs`, and `parallelizable_groups`
- estimate provenance fields such as `estimate_source`,
  `estimate_basis_effort`, and `estimate_basis_sample_size`
- claim-derived runtime facts such as `actual_task_seconds`,
  `actual_task_minutes`, `actual_active_minutes`,
  `actual_elapsed_minutes`, `claim_count`, `actual_reclaim_count`,
  `actual_worktrees_used`,
  `actual_retry_count`, `actual_handoffs`, and
  `actual_landing_failures`
- estimate-vs-actual comparison fields such as
  `estimate_delta_minutes` and `estimate_accuracy_ratio`
- best-effort changed-path capture via `changed_paths`,
  `actual_touched_paths`, and `actual_touched_path_count` when
  `result record` runs inside a git checkout with visible task changes
- prompt/context metrics derived from the task contract such as
  `context_doc_count`, `context_check_count`, `context_path_count`,
  `context_domain_count`, `context_has_objective`,
  `context_has_why`, `context_has_evidence`,
  `context_has_safe_first_slice`, `context_estimate_field_count`,
  `context_packet_score`, `context_packet_bytes`,
  `context_efficiency_ratio`, `misstep_total`, and
  `document_routing_value_score`

These newer context and document-routing fields are intentionally proxy
signals. They measure how much structured context Blackdog routed into a
task and how that context lined up with retries, land failures, and
estimate drift; they do not claim to prove that an agent literally read
or cited a specific doc.

When new tasks are added without explicit estimate values, Blackdog now
seeds task-shaping defaults from completed-history calibration and
stores the provenance fields above so later tune runs can distinguish
history-backed estimates from fallback defaults.

`metadata` is optional in older rows and continues to carry child-launch metadata.

## `<control_dir>/supervisor-runs/*/<task-id>/metadata.json`

Per-child launch artifact written by the supervisor before child execution.

Required keys:

- `run_id`
- `task_id`
- `child_agent`
- `workspace`
- `workspace_mode`
- `prompt_file`
- `stdout_file`
- `stderr_file`
- `launched_at`
- `metadata_file`
- `launch_command`
- `launch_command_strategy`
- `launch_settings`
- `prompt_template_version`
- `prompt_template_hash`
- `prompt_hash`

Optional keys:

- `worktree_spec`

## `<control_dir>/supervisor-runs/*/status.json`

Run status snapshot written by `blackdog supervise run`.

Current keys:

- `run_id`
- `actor`
- `workspace_mode`
- `poll_interval_seconds`
- `draining`
- `run_dir`
- `status_file`
- `supervisor_pid`
- `last_checked_at`
- `launch_command`
- `launch_overrides`
- `launch_settings`
- `steps`
- `recovery_actions`
- `completed_at`
- `final_status`
- `stopped_by_message_id`

Each `steps` entry may also include `recovery_actions` when the supervisor resolved dirty-primary state before the next launch window.

### `supervisor-runs/*/status.json` run state machine

`blackdog` freezes supervisor step states to
`blackdog.supervisor.SUPERVISOR_RUN_STEP_STATUSES`:

- `swept`: the run opened with a cleanup sweep and wave compaction pass.
- `running`: the supervisor is launching or waiting on child work.
- `draining`: a stop control has been accepted and the run is waiting only for
  already-launched work to finish.
- `idle`: the run exited cleanly because no runnable or running work remained.
- `stopped`: the run exited after draining in response to a stop control.

The externally reported current run status is normalized to
`blackdog.supervisor.SUPERVISOR_RUN_RUNTIME_STATUSES`:

- `running`, `draining`, `idle`, `stopped`, `interrupted`, or `historical`

Normalization rules:

- `swept` reads back as `running`
- legacy `complete` and `finished` snapshots read back as `idle`
- `interrupted` is derived at read time when no final status was persisted and
  the recorded supervisor pid is no longer alive

Per-child attempt states are frozen to
`blackdog.supervisor.SUPERVISOR_ATTEMPT_STATUSES`:

- `prepared`, `running`, `launch-failed`, `interrupted`, `blocked`, `failed`,
  `released`, `done`, `partial`, or `unknown`

Legacy child `final_task_status = "finished"` now normalizes to `done`.
Child `final_task_status = "open"` and legacy `claimed` outcomes normalize to
attempt status `partial`; `released` remains `released`.

Attempt/recovery statuses are:

- `prepared`
- `running`
- `launch-failed`
- `interrupted`
- `blocked`
- `failed`
- `released`
- `done`
- `unknown`

`child_finish.final_task_status = "finished"` is normalized to attempt status
`done` for recovery/report views so the same terminal outcome does not split
across two labels.

Recovery case values remain:

- `blocked_by_dirty_primary`
- `blocked_land`
- `partial_run`
- `landed_but_unfinished`

The shipped code freezes these literals in `blackdog.supervisor` through
`SUPERVISOR_RUN_STEP_STATUSES`, `SUPERVISOR_RUN_FINAL_STATUSES`,
`SUPERVISOR_RUN_RUNTIME_STATUSES`, `SUPERVISOR_ATTEMPT_STATUSES`, and
`SUPERVISOR_RECOVERY_CASES`.

## Delegated child telemetry and startup metrics

Use these artifacts and payloads to measure delegated-child ergonomics:

- `blackdog supervise status --format json` for current run state,
  actual latest-run launch settings, and pre-launch recovery context.
- `blackdog supervise recover --format json` for blocked/partial child
  execution and explicit recoverable cases.
- `blackdog supervise report --format json` for startup friction, retry
  pressure, output-shape consistency, and landing outcomes.
- In the child workspace, use `blackdog-child` for protocol commands:
  `result record`, `inbox`, and `release`.
- `supervise report` payload fields:
  - `summary.startup`, `summary.retry`, `summary.output_shape`, `summary.landing`
  - `runs[*].launch_settings`
  - `runs[*].attempts[*].launch_error`, `artifacts_dir`, `prompt_exists`,
    `stdout_exists`, `stderr_exists`, `metadata_exists`,
    `artifact_count`, `artifact_complete`, `metadata_valid`
  - `runs[*].attempts[*].launch_settings`
  - `runs[*].attempts[*].branch_ahead`, `landed`, `land_error`
  - `observations` with actionable severity buckets
  - `observations[*].category` values include
    `startup_friction`, `retry_pressure`, `output_shape_consistency`,
    `landing_failures`
- Child artifacts in `supervisor-runs/*/<task-id>/` and direct/manual
  landed diff artifacts in `task-results/<task-id>/` are the
  source-of-truth artifacts for startup and landing diagnostics.

## `blackdog snapshot`

Canonical static-page snapshot payload.

This is the same JSON payload embedded into the repo-branded backlog HTML file (and the compatibility `backlog-index.html` alias). The payload is Blackdog-product/UI owned even though it now embeds a neutral core export at `runtime_snapshot`. The shipped board reads its repo/header, plan/lane, and next-runnable contract surfaces through that neutral export; duplicated top-level aliases remain compatibility fields around the board projection.

Representative top-level keys include:

- `project_name`
- `project_root`
- `control_dir`
- `profile_file`
- `runtime_snapshot`
- `workspace_contract`
- `board_tasks`
- `tasks`
- `queue_status`
- `recent_results`
- `links`

`runtime_snapshot` is the stable machine contract that `blackdog_core` now owns. It is intentionally distinct from the board/editor projection around it.

Current `runtime_snapshot` keys include:

- `schema_version`
- `generated_at`
- `project_name`
- `project_root`
- `control_dir`
- `profile_file`
- `headers`
- `counts`
- `total`
- `workset`
- `task_dag`
- `push_objective`
- `release_gates`
- `objectives`
- `next_rows`
- `open_messages`
- `plan`
- `tasks`
- `runtime_model`
- `task_attempts`
- `wait_conditions`
- `control_messages`
- `workset_execution`
- `prompt_receipts`

Current `runtime_snapshot.workset` keys include:

- `id`
- `title`
- `visibility`
- `scope`
- `source_backlog_file`
- `source_kind`
- `target_branch`
- `target_commit`
- `task_count`
- `total_task_count`
- `omitted_task_count`
- `status_counts`
- `task_ids`
- `root_task_ids`

`runtime_snapshot.workset` is the canonical place to discover request-scoped focus. When a caller requests a focused view, `visibility` becomes `focused`, `scope.kind` becomes `task_ids`, and `scope.task_ids` echoes the requested selector ids. `task_ids` still names the visible slice after Blackdog adds any predecessor or lower-wave blocker closure needed to keep current runnable/blocking semantics coherent. `total_task_count` and `omitted_task_count` distinguish this partial read from the full backlog without implying any durable backlog mutation.

Current `runtime_snapshot.tasks[*]` keys include durable backlog/runtime facts only:

- `id`
- `title`
- `status`
- `detail`
- `bucket`
- `priority`
- `risk`
- `effort`
- `objective`
- `objective_title`
- `epic_title`
- `lane_id`
- `lane_title`
- `wave`
- `domains`
- `safe_first_slice`
- `why`
- `evidence`
- `paths`
- `checks`
- `docs`
- `task_shaping`
- `predecessor_ids`
- `requires_approval`
- `approval_reason`
- `approval_status`
- `claim_status`
- `claimed_by`
- `claimed_at`
- `released_by`
- `released_at`
- `release_note`
- `completed_by`
- `completed_at`
- `completion_note`
- `latest_result_status`
- `latest_result_at`
- `latest_result_actor`

When `runtime_snapshot.workset.visibility == "focused"`, `runtime_snapshot.tasks[*]`, `task_dag`, `next_rows`, and `plan` all describe the same bounded slice. Omitted tasks are not deletions; they are outside the current request-scoped view.

`queue_status` contains board counter fields:
- `running`
- `waiting`
- `blocked`
- `last_sweep_completed`
- `completed_today`
- `completed_all_time`

## `blackdog worktree preflight --format json`

Structured preflight payload for WTAM implementation work.

Current keys include:

- `project_root`
- `repo_root`
- `cwd`
- `current_worktree`
- `current_branch`
- `current_is_primary`
- `primary_worktree`
- `primary_branch`
- `dirty`
- `implementation_dirty`
- `worktree_model`
- `workspace_mode`
- `target_branch`
- `primary_dirty`
- `primary_dirty_paths`
- `current_worktree_ve`
- `current_worktree_blackdog_path`
- `current_worktree_has_local_blackdog`
- `ve_expectation`
- `workspace_contract`
- `worktrees_dir`
- `worktrees_dir_inside_repo`
- `worktrees`

`workspace_contract` is the normalized WTAM contract reused across CLI, supervisor, and UI surfaces. It carries the resolved workspace mode, current and primary worktree identity, target branch, primary dirty state, workspace-local Blackdog path, and the per-worktree `.VE` expectation.

## `blackdog supervise status --format json`

Canonical chat-native supervisor inspection payload.

Current keys include:

- `actor`
- `latest_run`
- `workset`
- `workspace_contract`
- `prelaunch_recovery`
- `control_action`
- `open_control_messages`
- `active_attempts`
- `open_wait_conditions`
- `ready_tasks`
- `recent_results`

## `blackdog supervise recover --format json`

Canonical structured recovery payload for interrupted/blocked supervisor runs.

Current keys include:

- `actor`
- `latest_run`
- `workspace_contract`
- `runs`
- `recoverable_cases`

Each `runs` entry includes:

- `run_id`
- `status`
- `workspace_mode`
- `draining`
- `run_dir`
- `status_file`
- `step_count`
- `children`

Each `children` entry includes child execution data from event replay, including:

- `run_id`
- `task_id`
- `child_agent`
- `workspace_mode`
- `task_branch`
- `target_branch`
- `primary_worktree`
- `workspace`
- `pid`
- `run_status`
- `final_task_status`
- `branch_ahead`
- `landed`
- `land_error`
- `exit_code`
- `missing_process`
- `claim_status`
- `run_dir`
- `child_artifact_dir`
- optional `recovery_case`

Each `recoverable_cases` entry now also includes:

- `target_branch`
- `primary_worktree`
- `child_artifact_dir`

When a case is recoverable, `recovery_case` includes:

- `case`
- `severity`
- `summary`
- `next_actions`

Known `case` values today:

- `blocked_by_dirty_primary`
- `blocked_land`
- `partial_run`
- `landed_but_unfinished`

## `blackdog supervise report --format json`

Canonical operator-metrics payload for startup friction, retry pressure, output-shape consistency, and landing outcomes.

Current keys include:

- `actor`
- `run_limit`
- `runs`
- `summary`
- `observations`

`runs` entries include:

- `run_id`
- `actor`
- `workspace_mode`
- `final_status`
- `attempts`
- `run_dir`
- `status_file`
- `started_at`
- `completed_at`
- `step_count`
- `attempt_count`
- `launch_failures`
- `landed_count`

Each `attempts` entry includes:

  - `task_id`
  - `child_agent`
  - `attempted_at`
  - `workspace`
- `workspace_mode`
- `branch`
- `target_branch`
- `launch_error`
- `launched`
- `exit_code`
- `missing_process`
- `final_task_status`
- `result_recorded`
- `branch_ahead`
- `landed`
- `land_error`
  - `artifacts_dir`
  - `prompt_exists`
  - `stdout_exists`
  - `stderr_exists`
  - `metadata_exists`
  - `artifact_count`
  - `artifact_complete`
  - `metadata_valid`
  - `metadata_parse_error`
  - `metadata_prompt_hash`
- `output_shape_note`

`summary` includes:

- `runs_total`
- `startup`
- `retry`
- `output_shape`
- `landing`

`observations` entries include `category`, `severity`, `summary`, and `detail`.

## `blackdog worktree start --format json`

Structured worktree spec emitted by the CLI and reused in the `worktree_start` event payload.

Current keys:

- `task_id`
- `task_title`
- `task_slug`
- `branch`
- `base_ref`
- `base_commit`
- `target_branch`
- `worktree_path`
- `primary_worktree`
- `current_worktree`

## Static HTML snapshot contract

Embedded by `blackdog render` into the repo-branded backlog HTML file and the compatibility `backlog-index.html` alias, and printable with `blackdog snapshot`.

Top-level keys:

- `schema_version`
- `generated_at`
- `content_updated_at`
- `last_checked_at`
- `supervisor_last_checked_at`
- `project_name`
- `project_root`
- `control_dir`
- `profile_file`
- `runtime_snapshot`
- `workspace_contract`
- `headers`
- `hero_highlights`
- `last_activity`
- `counts`
- `total`
- `push_objective`
- `objectives`
- `focus_task_ids`
- `next_rows`
- `open_messages`
- `recent_results`
- `recent_events`
- `plan`
- `threads`
- `unattended_tuning`
- `tasks`
- `board_tasks`
- `graph`
- `active_tasks`
- `queue_status`
- `links`
- `grouping_guide`

`generated_at` is the snapshot creation timestamp.
`content_updated_at` is the latest event timestamp from the source backlog/events stream (or `generated_at` when no event timestamp is available).
`last_checked_at` is the latest supervisor heartbeat timestamp when present, falling back to `generated_at`.
The current static board consumes repo/header, plan/lane, and next-runnable data from `runtime_snapshot`; the duplicated top-level aliases remain for compatibility and board-local convenience.

`focus_task_ids` is a board-facing alias for the visible task ids when a focused snapshot was requested. It stays empty for the default unfocused snapshot. Treat `runtime_snapshot.workset.scope` as the canonical selector and `focus_task_ids` as compatibility/view state, not as durable backlog membership.

`queue_status` includes the counters used by the top-right status panel in the current static board:
- `running`: tasks currently executing (`operator_status_key == "running"`).
- `waiting`: tasks ready in the execution queue (`operator_status_key == "waiting"`).
- `blocked`: tasks blocked from progression (`operator_status_key == "blocked"` or `"failed"`).
- `last_sweep_completed`: number of task IDs removed by the most recent `supervisor_run_sweep` event.
- `completed_today`: count of completed tasks whose `completed_at` date is the current local date.
- `completed_all_time`: cumulative completed-task count in the snapshot.

Current `links` keys:

- `backlog`
- `state`
- `html`
- `events`
- `inbox`
- `results`
- `threads`

Current `graph` keys:

- `tasks`
- `edges`

Current `graph.tasks[*]` keys include task identity and planning fields plus derived operator metadata:

- `activity`
- `created_at`
- `claimed_by`
- `claimed_at`
- `completed_at`
- `released_at`
- `updated_at`
- `active_compute_seconds`
- `active_compute_label`
- `total_compute_seconds`
- `total_compute_label`
- `latest_result_status`
- `latest_result_at`
- `latest_result_href`
- `latest_result_dir_href`
- `latest_result_preview`
- `task_shaping`
- `latest_result_task_shaping_telemetry`
- `result_count`
- `conversation_threads`
- `conversation_thread_ids`
- `conversation_thread_count`
- `primary_conversation_thread_id`
- `primary_conversation_thread_title`
- `primary_conversation_entries_href`
- `primary_conversation_file_href`
- `latest_run_status`
- `run_dir_href`
- `prompt_href`
- `thread_href`
- `stdout_href`
- `stderr_href`
- `metadata_href`
- `diff_href`
- `diffstat_href`
- `workspace_mode`
- `task_branch`
- `target_branch`
- `child_agent`
- `task_commit`
- `task_commit_short`
- `task_commit_url`
- `task_commit_subject`
- `task_commit_author`
- `task_commit_at`
- `task_commit_message`
- `model_response`
- `model_response_html`
- `model_response_truncated`
- `landed_commit`
- `landed_commit_short`
- `landed_commit_url`
- `landed_commit_message`
- `landed_commit_subject`
- `landed_commit_author`
- `landed_commit_at`
- `latest_run_branch_ahead`
- `latest_run_landed`
- `latest_run_land_error`
- `operator_status`

The UI/task projection intentionally extends `runtime_snapshot.tasks[*]` with board-only fields such as `activity`, `conversation_*`, `operator_status*`, task/run artifact hrefs, model-response excerpts, and landing metadata. Those extensions are not part of the neutral core export contract.
- `operator_status_key`
- `operator_status_detail`
- `links`

The `activity` timeline now includes task-created events, so `created_at` and `updated_at` can be derived from the same event stream that drives claims, releases, results, and run transitions.

`diff_href` and `diffstat_href` may resolve either to supervisor-run
artifacts or to direct/manual landed artifacts under
`task-results/<task-id>/`.

`thread_href` points at the best available raw child transcript when
supervisor run artifacts exist. Blackdog currently prefers a non-empty
`stderr.log`, then falls back to `stdout.log`. Direct/manual WTAM tasks
may not have a `thread_href`.

Current `active_tasks[*]` keys summarize the operator-facing running/claimed view:

- `id`
- `title`
- `status`
- `lane_title`
- `epic_title`
- `created_at`
- `updated_at`
- `claimed_by`
- `claimed_at`
- `total_compute_seconds`
- `total_compute_label`
- `latest_result_status`
- `latest_result_href`
- `latest_run_status`
- `operator_status`
- `operator_status_key`
- `workspace_mode`
- `task_branch`
- `target_branch`
- `task_commit`
- `task_commit_short`
- `task_commit_url`
- `prompt_href`
- `thread_href`
- `stdout_href`
- `stderr_href`
- `run_dir_href`

Current `threads[*]` rows summarize saved project conversations:

- `id`
- `title`
- `status`
- `created_at`
- `created_by`
- `updated_at`
- `entry_count`
- `user_entry_count`
- `assistant_entry_count`
- `system_entry_count`
- `latest_entry_at`
- `latest_entry_role`
- `latest_entry_actor`
- `latest_entry_preview`
- `task_ids`
- `thread_dir_href`
- `thread_file_href`
- `entries_href`

Current `unattended_tuning` keys summarize tracked-host tuning posture:

- `recommendation`
- `coverage_gaps`
- `time`
- `missteps`
- `calibration`
- `tracked_repo_count`
- `observed_repo_count`
- `stale_repo_count`
- `finding_severity_counts`
- `focus_counts`
- `hosts`

Current `unattended_tuning.hosts[*]` rows include:

- `project_name`
- `project_root`
- `observed_at`
- `tune_focus`
- `tune_summary`
- `counts`
- `finding_total`
- `top_finding`

Current `hero_highlights` keys summarize the hero's workspace/activity strip:

- `branch`
- `commit`
- `latest_run`
- `active_task_time`
- `completed_task_time`
- `average_completed_task_time`
- `total_task_time`

Current `next_rows[*]` keys summarize the `Status` panel's next-in-line projection:

- `id`
- `title`
- `lane`
- `wave`
- `risk`

When a focused view is requested, `next_rows[*]` stays scoped to the same visible task slice as `runtime_snapshot.workset.task_ids`, `plan`, and `graph`. Blackdog preserves existing ordering/blocking rules inside that slice rather than post-filtering a global queue.

Layout projections:

- `Backlog Control` uses `project_name`, `push_objective`, `content_updated_at`, `last_checked_at`, `last_activity`, `workspace_contract`, `headers`, `hero_highlights`, and `links`.
- The control panel renders a compact branch/commit line, a fixed hero timing line (time since last check, time since last update, total time on sweep, total time on backlog), a progress bar, and artifact links.
- The board does not render an objectives table or release-gate panel in the static snapshot.
- `links.threads` points at the saved conversation-thread directory root.
- The dedicated `Unattended Tuning` band uses `unattended_tuning.recommendation`, aggregate runtime counters, tracked-host focus counts, and per-host summaries from the tracked-install registry.
- The `Status` panel uses focus-task status counts plus `next_rows` to surface finished, running, next, waiting, and blocked work.
- The `Execution Map` uses `plan.lanes`, `board_tasks`, and task metadata to keep live lanes and waves visible without search/filter chrome.
- `Completed Tasks` renders a flat list of the most recent completed cards by completion time.
- The browser renders a split `Backlog Control`/`Status` top row, a full-width `Unattended Tuning` row, then a split `Execution Map`/`Completed Tasks` row. Execution-map and completed-task cards stay clickable.
- Focused task rows derive from lane-assigned work in the snapshot so progress, next-focus selection, and the task reader stay grounded in the current runnable backlog.
- `recent_results` remains a compact recent-result feed in the snapshot for other consumers; the rendered board no longer depends on a dedicated completed-history panel.
- When supervisor run artifacts exist, the task reader can render a capped stdout-derived model response inline and expose landed-commit metadata, including a GitHub commit URL when the repo origin resolves to GitHub.
- When `links.inbox` is present, inbox artifacts remain available to the task reader and other snapshot consumers even though the main board no longer renders a dedicated inbox header link.
