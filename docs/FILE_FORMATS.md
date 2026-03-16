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

## `<control_dir>/backlog-state.json`

Structured execution state.

Top-level keys:

- `schema_version`
- `approval_tasks`
- `task_claims`

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
- `steps`
- `completed_at`
- `final_status`
- `stopped_by_message_id`

## `blackdog snapshot`

Canonical static-page snapshot payload.

This is the same JSON payload embedded into `backlog-index.html`.
Representative top-level keys include:

- `project_name`
- `project_root`
- `control_dir`
- `profile_file`
- `workspace_contract`
- `board_tasks`
- `tasks`
- `recent_results`
- `links`

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
- `headers`
- `hero_highlights`
- `last_activity`
- `counts`
- `total`
- `push_objective`
- `objectives`
- `objective_rows`
- `next_rows`
- `open_messages`
- `recent_results`
- `recent_events`
- `plan`
- `tasks`
- `board_tasks`
- `graph`
- `active_tasks`
- `links`
- `grouping_guide`

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

Current `hero_highlights` keys summarize the hero's workspace/activity strip:

- `branch`
- `commit`
- `latest_run`
- `time_on_task`

Current `objective_rows[*]` keys summarize the objective-first board contract:

- `key`
- `id`
- `title`
- `task_ids`
- `active_task_ids`
- `lane_ids`
- `lane_titles`
- `wave_ids`
- `total`
- `done`
- `remaining`
- `progress`

`objective_rows[*].progress` carries:

- `counts`
- `total`
- `complete`
- `remaining`
- `percent`

Current `next_rows[*]` keys summarize the "what's next" queue projection:

- `id`
- `title`
- `lane`
- `wave`
- `risk`

Layout projections:

- Hero metadata uses `project_name`, `push_objective`, `generated_at`, `last_activity`, `workspace_contract`, `headers`, `hero_highlights`, and `links`.
- Hero workspace metadata renders as key-value rows that can include `workspace_contract.target_branch`, `workspace_contract.workspace_mode`, `workspace_contract.primary_dirty`, `workspace_contract.workspace_has_local_blackdog`, `project_root`, `control_dir`, and the current target commit from `headers`.
- Objective cards use `objective_rows` and open the task reader through the lead task for each objective row.
- Overview cards use `objective_rows`, `next_rows`, `hero_highlights`, `last_activity`, `open_messages`, and `workspace_contract` to keep the current push, next slice, and coordination state visible.
- Domain chips aggregate `tasks[*].domains` across the full snapshot, including completed work.
- The browser renders a hero panel, an objective-card section, overview/domain panels, and one active-work `Backlog` execution map rather than a backlog/history split.
- The `Backlog` execution map uses `plan.lanes`, `board_tasks`, `objective_rows`, `open_messages`, and `links.inbox`. `board_tasks` retains every lane-assigned or objective-tagged task row in the snapshot, including completed rows.
- The rendered browser execution map filters out rows whose `operator_status_key` normalizes to `complete`, so the visible backlog stays focused on active work while completed rows still feed progress, domain, and reader surfaces.
- `recent_results` remains a compact recent-result feed in the snapshot for other consumers; the rendered board no longer depends on a dedicated completed-history panel.
- When `links.inbox` is present, the `Backlog` header renders it as the `Inbox JSON` text link with the current open-message count from `open_messages`; when it is absent, the link stays hidden.
