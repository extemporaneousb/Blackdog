# File Formats

Mutable runtime state now lives under one shared local control root across worktrees. By default, `paths.control_dir = "@git-common/blackdog"` resolves to `<git-common-dir>/blackdog`, which is shared by the primary checkout and all linked worktrees.

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
  - `workspace_mode`
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
- With `workspace_mode = "git-worktree"`, child workspaces are branch-backed task worktrees created from the primary worktree branch.
- Blackdog may auto-stash uncommitted primary-worktree changes when branch landing remains blocked after retry/warning handling so the supervisor loop can keep moving.
- Successful branch-backed child runs are landed through the primary worktree with fast-forward semantics, and the supervisor completes the task after a successful land.

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

## `<control_dir>/supervisor-runs/ui-server.json`

State snapshot written by `blackdog ui serve`.

Current keys:

- `url`
- `host`
- `port`
- `snapshot_url`
- `stream_url`
- `project_name`
- `project_root`
- `control_dir`
- `state_file`
- `started_at`
- `pid`

## `blackdog ui snapshot`

Canonical readonly UI payload.

Current top-level identity keys:

- `project_name`
- `project_root`
- `control_dir`
- `profile_file`

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

## `<control_dir>/supervisor-runs/ui-server.json`

Ephemeral state file written by `blackdog ui serve`.

Current keys:

- `host`
- `port`
- `url`
- `snapshot_url`
- `stream_url`
- `state_file`
- `started_at`
- `pid`

## Live UI snapshot contract

Served by `blackdog ui snapshot` and `blackdog ui serve` at `/api/snapshot`.

Top-level keys:

- `schema_version`
- `generated_at`
- `project_name`
- `counts`
- `total`
- `push_objective`
- `objectives`
- `next_rows`
- `open_messages`
- `recent_results`
- `recent_events`
- `plan`
- `graph`
- `supervisor`
- `links`

Current `graph` keys:

- `tasks`
- `edges`

Current `supervisor` keys:

- `active_runs`
- `recent_runs`
- `loops`
