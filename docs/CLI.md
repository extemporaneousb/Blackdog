# CLI Reference

The current CLI covers the backlog runtime, a draining supervisor runner, and a static backlog board renderer.

When a repo keeps Blackdog in a repo-local virtual environment, prefer that entrypoint (for example `./.VE/bin/blackdog`) over a different `blackdog` on `PATH`.

## `blackdog`

### Project setup

- `blackdog create-project --project-root PATH --project-name NAME`
- `blackdog bootstrap --project-root PATH --project-name NAME`
- `blackdog refresh [--project-root PATH]`
- `blackdog update-repo PATH [--blackdog-source PATH]`
- `blackdog installs add|list|remove|update|observe ...`
- `blackdog init --project-root PATH --project-name NAME`
- `blackdog validate`
- `blackdog render`
- `blackdog snapshot`
- `blackdog coverage [--command CMD] [--output FILE]`
- `blackdog prompt [--complexity low|medium|high] [--format text|json] PROMPT...`
- `blackdog thread new|list|show|append|prompt|task ...`
- `blackdog task edit|run ...`
- `blackdog worktree preflight`
- `blackdog worktree start --id TASK`
- `blackdog worktree land [--id TASK] [--branch BRANCH] [--into TARGET]`
- `blackdog worktree cleanup --id TASK|--path PATH`

Use `blackdog create-project` when you want Blackdog to create a brand-new git repo, install itself into that repo's `.VE`, and bootstrap the local contract in one step. Use `blackdog bootstrap` for normal host-repo adoption into an existing repo. Use `blackdog refresh` when the host repo already has Blackdog installed and you want to regenerate the branded board plus managed project-local skill files without overwriting locally modified managed files. Use `blackdog update-repo` from a Blackdog source checkout when you want to reinstall Blackdog into another repo's `.VE` and immediately run that same refresh flow. Use `blackdog init` only when you want the repo-local artifact set without generating the project-local skill scaffold.

### Installation and host bootstrap

For a brand-new host repo from the current Blackdog checkout:

```bash
blackdog create-project --project-root /path/to/repo --project-name "Repo Name"
```

`create-project` expects a new or empty target directory. It creates the directory, initializes git, creates `.VE/`, installs Blackdog from the current checkout into that environment, and then runs the existing bootstrap scaffold so the new repo already has `blackdog.toml`, `AGENTS.md`, and `.codex/skills/<skill-name>/`.

For an existing host repo, install Blackdog first using one of:

- `python -m pip install -e /path/to/blackdog`
- `python -m pip install git+<github-url>`

Then run bootstrap in that repo:

```bash
cd /path/to/repo
blackdog bootstrap --project-name "Repo Name"
```

Bootstrap creates the project-local discovery files under:

- `.codex/skills/<skill-name>/SKILL.md`
- `.codex/skills/<skill-name>/agents/openai.yaml`
- `.codex/skills/<skill-name>/.blackdog-managed.json`

By default `<skill-name>` is `blackdog-<project-slug>`, so host repos get a project-specific wrapper skill instead of the generic `blackdog` token.
Codex surfaces that project-local token from the `agents/openai.yaml` file in the opened repository tree.
If the repo was open before bootstrap, reopen the repo (or restart the Codex session) so discovery picks up the new files.

After bootstrap, the default rendered board lives at `<control_dir>/<project-slug>-backlog.html`.
Blackdog also keeps a compatibility copy at `<control_dir>/backlog-index.html` so older bookmarks and docs do not break immediately.

When the installed Blackdog package changes, run:

```bash
blackdog refresh
```

`refresh` rewrites the managed project-local skill files when they still match the last generated version and preserves locally modified managed files by leaving them in place and writing `*.blackdog-new` sidecars beside them.

From a Blackdog source checkout, you can push the latest source into another repo-local `.VE` and refresh that host repo in one step:

```bash
blackdog update-repo /path/to/repo
```

When you maintain multiple local Blackdog repos from one development checkout, register them in a machine-local install registry and operate on them as a set:

```bash
blackdog installs add /path/to/repo-one /path/to/repo-two
blackdog installs list
blackdog installs update --all
blackdog installs observe --all
```

`blackdog installs add` stores repo roots under the shared control root for the current development checkout. `installs update` pushes the current Blackdog source into those tracked repos by calling the same `update-repo` flow per target. `installs observe` reads each tracked repo's backlog and tune state, and now also emits host-integration findings for wrapper-skill naming, prompt metadata, WTAM guidance, and task-shaping/history signals so the dev checkout can mine local host-repo intelligence without baking those machine-local paths into the skill scaffold or the host repos themselves.

`blackdog worktree ...` is the implementation-work entrypoint. WTAM is the implementation model:

- implementation work should happen from a branch-backed task worktree, not the primary checkout
- `blackdog worktree start` creates a task branch from the primary worktree branch and returns a structured worktree spec
- `blackdog worktree land` fast-forwards that task branch into the target branch, can remove the task worktree with `--cleanup`, and records task-scoped landed metadata when the branch belongs to a Blackdog task (or when `--id` is supplied explicitly)
- `blackdog worktree cleanup` removes a landed task worktree and, when explicitly told, deletes the associated branch
- `blackdog worktree preflight` reports the central project root, the actual current `cwd` and worktree, the primary worktree, configured worktree base, whether there are implementation-blocking local changes, the enforced WTAM workspace contract, the target branch, primary-worktree landing cleanliness, and the per-worktree `.VE` rule/CLI path for the current checkout

For Blackdog's own repo, manual-first is the default operating mode
until the runtime-hardening tasks land. Prefer the direct
`blackdog claim` -> `blackdog worktree preflight|start` ->
`blackdog result record` -> `land`/`blackdog complete` flow for normal
Blackdog-on-Blackdog work.

If you are running as a delegated child workspace, follow the delegated
startup protocol:

- the task is already claimed by the supervisor;
- the child task branch is already checked out in this workspace;
- the committed repository state is the delegated baseline;
- use workspace-local `./.VE/bin/blackdog` when available;
- record work with `blackdog result record`;
- do not run `land` or `complete` locally.

Use `blackdog supervise run` when you are explicitly exercising delegated
execution or supervisor behavior, not as the required path to continue
product development.

### Backlog management

- `blackdog backlog new NAME`
- `blackdog backlog remove NAME`
- `blackdog backlog reset`
- `blackdog add --title ... --bucket ... --why ... --evidence ... --safe-first-slice ... [--task-shaping JSON_OBJECT]`
- `blackdog remove --id TASK --actor NAME`
- `blackdog plan`
- `blackdog summary`
- `blackdog next`
- `blackdog prompt`
- `blackdog thread new|list|show|append|prompt|task`
- `blackdog task edit|run`
- `blackdog tune`
- `blackdog supervise run`
- `blackdog supervise sweep`
- `blackdog supervise status`
- `blackdog supervise recover`
- `blackdog supervise report`
- `blackdog claim --agent NAME [--pid PID]`
- `blackdog release --id TASK --agent NAME`
- `blackdog complete --id TASK --agent NAME`
- `blackdog decide --id TASK --agent NAME --decision approved|denied|deferred|done`
- `blackdog comment --actor NAME --id TASK --body ...`
- `blackdog events`

`blackdog prompt` rewrites a raw prompt against the local repo contract. It emits low/medium/high complexity prompt profiles derived from the repo profile, routed docs, validation defaults, and the latest tune recommendation so host-repo skills can reuse Blackdog's repo-local guidance instead of rebuilding it from scratch.

`blackdog thread ...` manages saved freeform conversation threads under the shared control root. Use it when the operator should write normal markdown conversation instead of filling a structured task-spec template:

- `thread new` creates a conversation thread, optionally with the first user entry.
- `thread append` adds one user/assistant/system entry, preserving timestamp, actor, optional task link, and optional response duration.
- `thread prompt` rewrites the saved conversation thread against the repo-local prompt contract.
- `thread task` creates one backlog task from the saved conversation and links that task back to the thread.
- `thread list` and `thread show` are the read surfaces for Emacs and shell operators.

The prompt profiles now also carry calibrated task-shaping defaults by effort (`S`/`M`/`L`) derived from completed work in the repo. That lets tune improve prompt generation, not just reporting: prompts can ask for explicit estimate snapshots that match the repo's observed task history instead of generic defaults.

`blackdog remove` deletes a task from the backlog plan and task block when execution has not materially started. Removal is intentionally conservative:

- active claimed tasks cannot be removed
- completed tasks cannot be removed
- tasks with recorded result artifacts cannot be removed
- released or never-claimed tasks can be removed, and the runtime clears any approval or claim state that still points at that task

The command appends a `task_removed` event and rerenders the static board like other task-state mutations.

`blackdog task edit` is the in-place mutation surface for thin UIs. It preserves the task id, updates the task block plus plan assignment, and rejects tasks that already have claim state or recorded results. Use it for operator edits before execution starts instead of teaching editors to rewrite backlog markdown directly.

`blackdog task run` is the manual WTAM preparation surface. It claims the task for the selected agent, creates the default branch-backed worktree when one does not exist yet, or reuses the existing task worktree when it is already present. Pass `--format json` when an editor wants the returned branch/worktree contract instead of parsing shell text.

`blackdog tune` now does direct tuning as well as optional backlog seeding. It still prints the stable self-tuning task payload when task creation is enabled, but it also emits a `tune_analysis` summary plus low/medium/high `prompt_profiles`. The analysis now groups runtime signals into `time`, `missteps`, `document_use_value`, and `context_efficiency`, then uses those categories to decide which runtime gap should be addressed first. Pass `--no-task` when you want the tuning guidance without automatically seeding a backlog task.

`blackdog add` now auto-seeds missing task-shaping estimates from the repo's completed task history. When you omit `--task-shaping` or leave estimate fields blank, Blackdog preserves comparable fields like `estimated_elapsed_minutes`, `estimated_active_minutes`, `estimated_validation_minutes`, and `estimated_touched_paths` so future `tune` runs can compare new work against the same contract instead of mostly reporting coverage gaps.

`blackdog backlog new NAME` creates a separate backlog artifact set under the configured control root. It uses the same file layout as the default backlog and is intended for scratch queues, test fixtures, or alternate operator views without polluting the default backlog.

`blackdog backlog remove NAME` deletes one of those named backlog artifact sets.

`blackdog backlog reset` obliterates the mutable state for the default backlog and recreates a fresh empty runtime. Use `--purge-named` when you also want to remove every named backlog under `<control_dir>/backlogs/`.

`blackdog supervise run` now assumes an exec-capable Codex launcher. With the default config, it prefers the Codex.app runtime when available and no longer supports the legacy prompt launcher. When an editor or operator needs a one-off model or reasoning override, use `--model MODEL` and `--reasoning-effort low|medium|high|xhigh`; Blackdog rewrites the launch prefix itself so UIs do not need to know Codex argv details.

The default `worktrees_dir` is now `../.worktrees`, which keeps Blackdog task worktrees as siblings of the primary checkout rather than nesting them under repo-controlled runtime artifacts. If a repo prefers a `.worktrees` symlink inside the repo root, Blackdog will follow that resolved path when it is configured in `blackdog.toml`.

Blackdog creates branch-backed child worktrees from the primary worktree branch and treats committed repo state as the delegated baseline. If landing is blocked by dirty primary-worktree changes, the supervisor records the blocked child outcome first, then runs a pre-launch recovery gate before any further child launch. That gate can land the blocked branch after the primary checkout is clean, commit a primary checkout that already matches the blocked branch tree, or stash unrelated dirty primary changes into a follow-up backlog task so the queue can resume safely. Child agents are expected to commit on their task branch, and the supervisor lands that branch through the primary worktree with fast-forward semantics before completing the task.

Completed outcomes now encode one of three landing states:

- Successful land: child artifacts include a landed commit and the task/card reader can render a `Landed` badge linked to the commit page.
- No-op completion: no committable work happened and no landed badge is shown.
- Blocked land: branch work was ahead of target but could not be landed; the task remains blocked so dependent work can continue to detect the upstream failure.

The generated child prompt tells the agent that committed repo state is the baseline, that the task is already claimed by the supervisor, that it must commit changes on the task branch, and that Blackdog CLI output should be treated as the source of truth for backlog state. It also surfaces the run workspace mode, the task branch to target-branch landing path, the primary-worktree cleanliness gate, and the per-worktree `.VE` rule. When the current workspace contains `.VE/bin/blackdog`, the prompt points child agents at that workspace-local CLI; otherwise it falls back to `blackdog` from the active environment and tells the agent to bootstrap `./.VE` in that worktree rather than reusing another worktree's environment.

`blackdog supervise run` is the draining supervisor mode. It performs one cleanup sweep at the start of the run, removes already-completed tasks from the execution map, drops empty lanes and waves, compacts remaining waves back to small integers, then keeps rereading backlog/state while the run is active. Before it launches new child work from an idle point in that loop, it runs the primary-worktree recovery gate described above and records any resulting `stash`, `land`, or `commit` recovery actions in the run payload. Tasks completed during that active run stay visible in place on the execution map until the next run starts and sweeps them away. If there is no runnable or running work after that opening sweep, the command returns `idle` immediately instead of waiting for future tasks. Otherwise the run exits only when it reaches idle or when a `stop` inbox message puts it into draining mode. `stop` prevents new launches but does not interrupt already-running child tasks.

`blackdog supervise sweep` is the non-draining counterpart. It runs the same cleanup/liveness refresh once, rerenders the board when the plan changed, and returns the current ready queue plus launch-default metadata without starting child work.

While a run is active, Blackdog keeps writing the latest `status.json` for that run plus per-child `prompt.txt`, `stdout.log`, `stderr.log`, `metadata.json`, and any diff artifacts under `supervisor-runs/<timestamp-runid>/`. Operator UIs such as the Emacs monitor can poll that status file and tail those child artifacts directly instead of waiting for the final `supervise run` process output.

Claimed tasks no longer have a lease timeout. `blackdog claim` can record the long-lived claiming process with `--pid PID`, and supervisor child claims record that pid automatically. A live claimed process can run indefinitely; the supervisor only recovers a claim when the reported claiming pid is missing on repeated liveness scans, and even then it releases the claim instead of killing a still-live task.

`blackdog supervise status` is the chat-native inspection surface for that run. It reports the latest saved run status for a supervisor actor, the resolved WTAM workspace contract, the current launch defaults (including parsed model/reasoning when configured), the current pre-launch recovery decision when one exists, the currently open `stop` control messages for that actor, the current ready-task queue, and the most recent supervisor or child-agent task results in one compact text or JSON view.

`blackdog supervise recover` is the structured interruption-recovery surface. It reports recent supervisor runs and child executions, and highlights recoverable cases with suggested follow-up actions. Recoverable dirty-primary rows now include the branch/primary metadata the supervisor uses for the pre-launch recovery gate. Use this command to inspect what the supervisor will try to land, commit, or stash before deciding whether to relaunch, clean up, retry, or complete a replacement flow yourself.

`blackdog supervise report` is the operator metrics surface. It reads historical supervisor events/status/results and summarizes startup friction (launch pressure/failures), retry pressure (task re-run rate), output-shape consistency (expected artifact presence), and landing outcomes (landing failures and success). This report is read-only and intended for quick ergonomics diagnostics across the most recent runs.

`blackdog snapshot` prints the canonical JSON contract embedded into the static repo-branded backlog HTML page (by default `<project-slug>-backlog.html`, with a compatibility copy at `backlog-index.html`). That payload drives the current board: the project-branded hero, `Status` counters (running, waiting, blocked, last sweep completed, completed today, completed all-time), the live `Execution Map`, and completed-task list. It includes repo identity (`project_name`, `project_root`, `control_dir`), the current WTAM workspace contract, render headers, hero highlights (`branch`, `commit`, `latest_run`, `active_task_time`, `completed_task_time`, `average_completed_task_time`, `total_task_time`), `content_updated_at` (derived from the latest snapshot event timestamp), the board-facing `last_checked_at` (derived from the latest supervisor check heartbeat), the raw `supervisor_last_checked_at` heartbeat when available, the latest recorded activity actor/timestamp, backlog counts, push/objective metadata, next-focus rows, graph nodes and dependency edges, per-task lifecycle timestamps (`created_at`, `updated_at`, `claimed_at`, `completed_at`, `released_at`), task and landed commit metadata, per-task compute/result/run metadata, markdown-rendered model-response excerpts, open inbox messages, direct artifact links, focus-task summaries, and recent task-result summaries.

The snapshot now also includes project-level `threads` rows plus per-task conversation linkage (`conversation_threads`, `conversation_thread_ids`, and `primary_conversation_*` fields) so Emacs can move directly from a saved operator conversation to its derived task and back again.

`blackdog render` writes the static repo-branded backlog HTML page under the configured control root and refreshes the compatibility `backlog-index.html` copy beside it. Blackdog CLI writes and active supervisor runs rerender that page as part of normal state changes, including supervisor exit after landed task-state updates. The page embeds the current snapshot JSON directly, renders a wider control/status top band, the live execution map, and a completed-task card list, keeps artifact navigation as plain links, and opens execution/history cards in the task reader. When a child run captured `stdout.log` and a landed commit, the reader also shows the inline model response plus a landed-commit link or message. Operators can still reload the file manually, and the hero header now includes an optional 30-second auto-reload toggle with a visible countdown.

### Structured results

- `blackdog result record --id TASK --actor NAME --status success|blocked|partial --what-changed ... --task-shaping-telemetry JSON_OBJECT ...`

`result record` now merges operator-supplied `--task-shaping-telemetry` with the runtime facts Blackdog can derive automatically: the current task-shaping estimate snapshot, aggregate task time derived from claims, reclaim count, worktree count, retry count, landing-failure count, best-effort changed-paths from the current git checkout, and prompt-tuning/context metrics derived from the task contract (`context_doc_count`, `context_check_count`, `context_path_count`, `context_packet_score`, `misstep_total`, and related fields).

When a task is linked to one or more saved conversation threads, `result record` also appends an assistant entry to those threads using the result summary and any recorded runtime duration.

### Coverage reporting

`blackdog coverage` runs one or more validation commands (defaulting to profile `taxonomy.validation_commands`) under the stdlib `trace` module and emits a JSON summary with module-level coverage and aggregated totals.

- `--command` uses the supplied validation command instead of profile defaults.
- `--output` writes the same JSON report to disk for retention.
- Return code is non-zero when any validation command fails.

### Delegated child telemetry workflow

For delegated ergonomics reviews, use:

- `blackdog supervise report --format json`
- `blackdog supervise recover --format json`
- `blackdog supervise status --format json`

When child work happens, prefer the generated protocol helper in the child
workspace (`blackdog-child`) for protocol operations:

- `blackdog-child result record --status ... --task-shaping-telemetry JSON_OBJECT`
- `blackdog-child inbox list ...`
- `blackdog-child release --note ...`

Use these payloads to check:

- startup friction (`summary.startup`, `observations` with category `startup`)
- retry pressure (`summary.retry`, launch re-runs)
- output-shape consistency (`attempts[*].artifact_complete`, `attempts[*].artifact_count`,
  `attempts[*].output_shape_note`, `output_shape` summary)
- landing outcomes (`summary.landing`, `runs[*].landed_count`, `attempts[*].land_error`)
- artifact telemetry observations (`observations` with category `startup_friction`,
  `retry_pressure`, `output_shape_consistency`, `landing_failures`)

Cross-check attempt-level fields (`prompt_exists`, `stdout_exists`,
`stderr_exists`, `metadata_exists`) against `supervisor-runs` artifacts
before changing launch settings or child startup contract details.

### Inbox

- `blackdog inbox send --sender NAME --recipient NAME --body ...`
- `blackdog inbox list`
- `blackdog inbox resolve --message-id ID --actor NAME`

## `blackdog-skill`

- `blackdog-skill new backlog --project-root PATH`
- `blackdog-skill refresh backlog --project-root PATH`

`blackdog bootstrap` is now the preferred one-command host-repo entrypoint. `blackdog-skill new backlog` remains as a compatibility wrapper that ensures the project has a Blackdog profile/artifact set and a project-local skill under `.codex/skills/<skill-name>/`.

`blackdog-skill refresh backlog` remains as a compatibility wrapper around the managed skill refresh flow. It regenerates the project-local skill files from the current `blackdog.toml` profile without rebuilding backlog/runtime files, preserves locally modified managed files by writing `*.blackdog-new` sidecars, and is now usually superseded by `blackdog refresh`.
