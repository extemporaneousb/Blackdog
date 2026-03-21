# File Formats

Mutable runtime state now lives under one shared local control root across worktrees. By default, `paths.control_dir = "@git-common/blackdog"` resolves to `<git-common-dir>/blackdog`, which is shared by the primary checkout and all linked worktrees.

The near-term contract is intentionally one format. The default backlog lives at `<control_dir>/...`, and any named backlog lives at `<control_dir>/backlogs/<slug>/...` using the exact same file set. Blackdog does not use a separate test-only schema.

By default, the rendered HTML board is repo-branded:

- default backlog: `<control_dir>/<project-slug>-backlog.html`
- named backlog: `<control_dir>/backlogs/<slug>/<project-slug>-<slug>-backlog.html`
- compatibility alias: `backlog-index.html` beside the rendered HTML file

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
- `summary`: aggregated coverage totals for modules under `src/`

`runs[*].coverage` maps `module_path` → `{covered, total, coverage_percent}`.
`summary` also includes:

- `modules`
- `module_count`
- `total_lines`
- `covered_lines`
- `coverage_percent`

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

## `.codex/skills/blackdog/.blackdog-managed.json`

Project-local managed-skill manifest.

Written by `blackdog bootstrap`, `blackdog refresh`, `blackdog update-repo`, and `blackdog-skill refresh backlog`.

Schema:

- `schema_version`
- `files`
  - `<relative path>` → `{ "sha256": "<generated-content-hash>" }`

Blackdog uses this manifest to tell whether a managed skill file still matches the last generated version. If the file diverged locally, refresh leaves it in place and writes a `*.blackdog-new` sidecar beside it instead of overwriting it.

## `<control_dir>/backlog-state.json`

Structured execution state.

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
- `claim`
- `release`
- `complete`
- `decision`
- `comment`
- `render`
- `message`
- `message_resolved`
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
- `prompt_template_version`
- `prompt_template_hash`
- `prompt_hash`

`launch_command` is the resolved argv prefix and includes prompt-mode launch behavior (for example, replacing default `codex` with desktop `Codex.app`), while `prompt_*` fields capture the child prompt fingerprint used for that run.

`worktree_land` event payloads carry the landed branch outcome for direct/manual WTAM flow:

- `branch`
- `target_branch`
- `primary_worktree`
- `target_worktree`
- `landed_commit`

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
- claim-derived runtime facts such as `actual_task_seconds`,
  `actual_task_minutes`, `actual_active_minutes`, `claim_count`,
  `actual_reclaim_count`, `actual_worktrees_used`,
  `actual_retry_count`, `actual_handoffs`, and
  `actual_landing_failures`
- best-effort changed-path capture via `changed_paths`,
  `actual_touched_paths`, and `actual_touched_path_count` when
  `result record` runs inside a git checkout with visible task changes

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
- `steps`
- `recovery_actions`
- `completed_at`
- `final_status`
- `stopped_by_message_id`

Each `steps` entry may also include `recovery_actions` when the supervisor resolved dirty-primary state before the next launch window.

## Delegated child telemetry and startup metrics

Use these artifacts and payloads to measure delegated-child ergonomics:

- `blackdog supervise status --format json` for current run state and
  pre-launch recovery context.
- `blackdog supervise recover --format json` for blocked/partial child
  execution and explicit recoverable cases.
- `blackdog supervise report --format json` for startup friction, retry
  pressure, output-shape consistency, and landing outcomes.
- In the child workspace, use `blackdog-child` for protocol commands:
  `result record`, `inbox`, and `release`.
- `supervise report` payload fields:
  - `summary.startup`, `summary.retry`, `summary.output_shape`, `summary.landing`
  - `runs[*].attempts[*].launch_error`, `artifacts_dir`, `prompt_exists`,
    `stdout_exists`, `stderr_exists`, `metadata_exists`,
    `artifact_count`, `artifact_complete`, `metadata_valid`
  - `runs[*].attempts[*].branch_ahead`, `landed`, `land_error`
  - `observations` with actionable severity buckets
  - `observations[*].category` values include
    `startup_friction`, `retry_pressure`, `output_shape_consistency`,
    `landing_failures`
- Child artifacts in `supervisor-runs/*/<task-id>/` and
  `task-results/<task-id>/` are the source-of-truth artifacts for
  startup and landing diagnostics.

## `blackdog snapshot`

Canonical static-page snapshot payload.

This is the same JSON payload embedded into the repo-branded backlog HTML file (and the compatibility `backlog-index.html` alias).
Representative top-level keys include:

- `project_name`
- `project_root`
- `control_dir`
- `profile_file`
- `workspace_contract`
- `board_tasks`
- `tasks`
- `queue_status`
- `recent_results`
- `links`

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
- `workspace_contract`
- `prelaunch_recovery`
- `control_action`
- `open_control_messages`
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
- `workspace_contract`
- `headers`
- `hero_highlights`
- `last_activity`
- `counts`
- `total`
- `push_objective`
- `objectives`
- `next_rows`
- `open_messages`
- `recent_results`
- `recent_events`
- `plan`
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

`queue_status` includes the counters used by the top-right status panel in the current static board:
- `running`: tasks currently executing (`operator_status_key == "running"`).
- `waiting`: tasks ready in the execution queue (`operator_status_key == "waiting"`).
- `blocked`: tasks blocked from progression (`operator_status_key == "blocked"` or `"failed"`).
- `last_sweep_completed`: number of task IDs removed by the most recent `supervisor_run_sweep` event.
- `completed_today`: count of completed tasks whose `completed_at` date is the current local date.
- `completed_all_time`: cumulative completed-task count in the snapshot.

Current `links` keys:

- `backlog`
- `html`
- `events`
- `inbox`
- `results`

Current `graph` keys:

- `tasks`
- `edges`

Current `graph.tasks[*]` keys include task identity and planning fields plus derived operator metadata:

- `activity`
- `claimed_by`
- `claimed_at`
- `completed_at`
- `released_at`
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
- `latest_run_status`
- `run_dir_href`
- `prompt_href`
- `stdout_href`
- `stderr_href`
- `metadata_href`
- `diff_href`
- `diffstat_href`
- `workspace_mode`
- `task_branch`
- `target_branch`
- `child_agent`
- `model_response`
- `model_response_html`
- `model_response_truncated`
- `landed_commit`
- `landed_commit_short`
- `landed_commit_url`
- `landed_commit_message`
- `latest_run_branch_ahead`
- `latest_run_landed`
- `latest_run_land_error`
- `operator_status`
- `operator_status_key`
- `operator_status_detail`
- `links`

Current `active_tasks[*]` keys summarize the operator-facing running/claimed view:

- `id`
- `title`
- `status`
- `lane_title`
- `epic_title`
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
- `prompt_href`
- `stdout_href`
- `stderr_href`
- `run_dir_href`

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

Layout projections:

- `Backlog Control` uses `project_name`, `push_objective`, `content_updated_at`, `last_checked_at`, `last_activity`, `workspace_contract`, `headers`, `hero_highlights`, and `links`.
- The control panel renders a compact branch/commit line, a fixed hero timing line (time since last check, time since last update, total time on sweep, total time on backlog), a progress bar, and artifact links.
- The board does not render an objectives table or release-gate panel in the static snapshot.
- The `Status` panel uses focus-task status counts plus `next_rows` to surface finished, running, next, waiting, and blocked work.
- The `Execution Map` uses `plan.lanes`, `board_tasks`, and task metadata to keep live lanes and waves visible without search/filter chrome.
- `Completed Tasks` renders a flat list of the most recent completed cards by completion time.
- The browser renders a split `Backlog Control`/`Status` top row, then a split `Execution Map`/`Completed Tasks` row. Execution-map and completed-task cards stay clickable.
- Focused task rows derive from lane-assigned work in the snapshot so progress, next-focus selection, and the task reader stay grounded in the current runnable backlog.
- `recent_results` remains a compact recent-result feed in the snapshot for other consumers; the rendered board no longer depends on a dedicated completed-history panel.
- When supervisor run artifacts exist, the task reader can render a capped stdout-derived model response inline and expose landed-commit metadata, including a GitHub commit URL when the repo origin resolves to GitHub.
- When `links.inbox` is present, inbox artifacts remain available to the task reader and other snapshot consumers even though the main board no longer renders a dedicated inbox header link.
