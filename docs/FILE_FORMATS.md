# File Formats

Mutable runtime state now lives under one shared local control root across worktrees. By default, `paths.control_dir = "@git-common/blackdog"` resolves to `<git-common-dir>/blackdog`, which is shared by the primary checkout and all linked worktrees.

The near-term contract is intentionally one format. The default backlog lives at `<control_dir>/...`, and any named backlog lives at `<control_dir>/backlogs/<slug>/...` using the exact same file set. Blackdog does not use a separate test-only schema.

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
  - `default_claim_lease_hours`
  - `require_claim_for_completion`
  - `auto_render_html`
- `[supervisor]`
  - `launch_command`
  - `max_parallel`
  - `task_timeout_seconds`
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
- `control_dir` accepts the special prefix `@git-common`, which resolves against `git rev-parse --git-common-dir`.
- The default `worktrees_dir` value is `../.worktrees`, which places branch-backed task worktrees beside the primary checkout by default.
- Child workspaces are branch-backed task worktrees created from the primary worktree branch.
- Dirty primary-worktree state blocks branch landing as a contract violation. The supervisor sends an inbox warning, records a blocked result, and leaves the child branch/worktree intact rather than auto-stashing the primary checkout.
- Successful branch-backed child runs are landed through the primary worktree with fast-forward semantics, and the supervisor completes the task after a successful land.

## `<control_dir>/backlogs/<slug>/...`

Named backlog root.

Each named backlog reuses the same artifact layout as the default backlog root:

- `backlog.md`
- `backlog-state.json`
- `events.jsonl`
- `inbox.jsonl`
- `task-results/`
- `backlog-index.html`
- `supervisor-runs/`

These named roots are created and removed with `blackdog backlog new NAME` and `blackdog backlog remove NAME`. The default CLI still operates on the default backlog unless a command explicitly targets a named root in the future.

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

## Planning model

- `task`: the executable unit. Claims, results, completion, and dependencies are tracked at task level.
- `epic`: a thematic grouping for related tasks. Epics organize reporting and intent; they do not control runnable order.
- `lane`: an ordered stream of tasks inside an epic or work area. Earlier tasks in a lane become predecessors of later tasks in that same lane.
- `wave`: the cross-lane activation boundary. Blackdog only considers the lowest unfinished wave runnable, so wave `1` waits for unfinished work in wave `0`.

In practice: `epic` answers "why this cluster exists", `lane` answers "what must happen in sequence", `wave` answers "which phase is currently open", and `task` is the unit an agent actually executes.

## `<control_dir>/backlog-state.json`

Structured execution state.

Top-level keys:

- `schema_version`
- `approval_tasks`
- `task_claims`

## `<control_dir>/events.jsonl`

Append-only event log.

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
- `child_launch`
- `child_launch_failed`
- `child_finish`
- `supervisor_run_finished`
- `supervisor_loop_started`
- `supervisor_loop_heartbeat`
- `supervisor_loop_finished`

## `<control_dir>/inbox.jsonl`

Append-only message channel.

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

## `<control_dir>/supervisor-runs/*/status.json`

Loop status snapshot written by `blackdog supervise loop`.

Current keys:

- `loop_id`
- `actor`
- `workspace_mode`
- `poll_interval_seconds`
- `max_cycles`
- `stop_when_idle`
- `loop_dir`
- `status_file`
- `cycles`
- `completed_at`
- `final_status`

## `blackdog snapshot`

Canonical static-page snapshot payload.

Current top-level identity keys:

- `project_name`
- `project_root`
- `control_dir`
- `profile_file`

## `blackdog worktree preflight --format json`

Structured preflight payload for WTAM implementation work.

Current keys include:

- `repo_root`
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
- `latest_loop`
- `workspace_contract`
- `control_action`
- `open_control_messages`
- `ready_tasks`
- `recent_results`

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

Embedded by `blackdog render` into `backlog-index.html` and printable with `blackdog snapshot`.

Top-level keys:

- `schema_version`
- `generated_at`
- `project_name`
- `project_root`
- `control_dir`
- `profile_file`
- `workspace_contract`
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
- `graph`
- `active_tasks`
- `links`
- `grouping_guide`

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
