# CLI Reference

The current CLI covers the backlog runtime, a draining supervisor runner, and a static backlog board renderer.

When a repo keeps Blackdog in a repo-local virtual environment, prefer that entrypoint (for example `./.VE/bin/blackdog`) over a different `blackdog` on `PATH`.

## `blackdog`

### Project setup

- `blackdog bootstrap --project-root PATH --project-name NAME`
- `blackdog init --project-root PATH --project-name NAME`
- `blackdog validate`
- `blackdog render`
- `blackdog snapshot`
- `blackdog coverage [--command CMD] [--output FILE]`
- `blackdog worktree preflight`
- `blackdog worktree start --id TASK`
- `blackdog worktree land [--id TASK] [--branch BRANCH] [--into TARGET]`
- `blackdog worktree cleanup --id TASK|--path PATH`

Use `blackdog bootstrap` for normal host-repo adoption. Use `blackdog init` only when you want the repo-local artifact set without generating the project-local skill scaffold.

### Installation and host bootstrap

For a fresh host repo, install Blackdog first using one of:

- `python -m pip install -e /path/to/blackdog`
- `python -m pip install git+<github-url>`

Then run bootstrap in that repo:

```bash
cd /path/to/repo
blackdog bootstrap --project-name "Repo Name"
```

Bootstrap creates the project-local discovery files under:

- `.codex/skills/blackdog/SKILL.md`
- `.codex/skills/blackdog/agents/openai.yaml`

Codex surfaces the `blackdog` skill from the `agents/openai.yaml` file in the opened repository tree.
If the repo was open before bootstrap, reopen the repo (or restart the Codex session) so discovery picks up the new files.

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
- `blackdog add --title ... --bucket ... --why ... --evidence ... --safe-first-slice ...`
- `blackdog plan`
- `blackdog summary`
- `blackdog next`
- `blackdog tune`
- `blackdog supervise run`
- `blackdog supervise status`
- `blackdog supervise recover`
- `blackdog supervise report`
- `blackdog claim --agent NAME [--pid PID]`
- `blackdog release --id TASK --agent NAME`
- `blackdog complete --id TASK --agent NAME`
- `blackdog decide --id TASK --agent NAME --decision approved|denied|deferred|done`
- `blackdog comment --actor NAME --id TASK --body ...`
- `blackdog events`

`blackdog backlog new NAME` creates a separate backlog artifact set under the configured control root. It uses the same file layout as the default backlog and is intended for scratch queues, test fixtures, or alternate operator views without polluting the default backlog.

`blackdog backlog remove NAME` deletes one of those named backlog artifact sets.

`blackdog backlog reset` obliterates the mutable state for the default backlog and recreates a fresh empty runtime. Use `--purge-named` when you also want to remove every named backlog under `<control_dir>/backlogs/`.

`blackdog supervise run` now assumes an exec-capable Codex launcher. With the default config, it prefers the Codex.app runtime when available and no longer supports the legacy prompt launcher.

The default `worktrees_dir` is now `../.worktrees`, which keeps Blackdog task worktrees as siblings of the primary checkout rather than nesting them under repo-controlled runtime artifacts. If a repo prefers a `.worktrees` symlink inside the repo root, Blackdog will follow that resolved path when it is configured in `blackdog.toml`.

Blackdog creates branch-backed child worktrees from the primary worktree branch and treats committed repo state as the delegated baseline. If landing is blocked by dirty primary-worktree changes, the supervisor records the blocked child outcome first, then runs a pre-launch recovery gate before any further child launch. That gate can land the blocked branch after the primary checkout is clean, commit a primary checkout that already matches the blocked branch tree, or stash unrelated dirty primary changes into a follow-up backlog task so the queue can resume safely. Child agents are expected to commit on their task branch, and the supervisor lands that branch through the primary worktree with fast-forward semantics before completing the task.

Completed outcomes now encode one of three landing states:

- Successful land: child artifacts include a landed commit and the task/card reader can render a `Landed` badge linked to the commit page.
- No-op completion: no committable work happened and no landed badge is shown.
- Blocked land: branch work was ahead of target but could not be landed; the task remains blocked so dependent work can continue to detect the upstream failure.

The generated child prompt tells the agent that committed repo state is the baseline, that the task is already claimed by the supervisor, that it must commit changes on the task branch, and that Blackdog CLI output should be treated as the source of truth for backlog state. It also surfaces the run workspace mode, the task branch to target-branch landing path, the primary-worktree cleanliness gate, and the per-worktree `.VE` rule. When the current workspace contains `.VE/bin/blackdog`, the prompt points child agents at that workspace-local CLI; otherwise it falls back to `blackdog` from the active environment and tells the agent to bootstrap `./.VE` in that worktree rather than reusing another worktree's environment.

`blackdog supervise run` is the only supervisor mode. It performs one cleanup sweep at the start of the run, removes already-completed tasks from the execution map, drops empty lanes and waves, compacts remaining waves back to small integers, then keeps rereading backlog/state while the run is active. Before it launches new child work from an idle point in that loop, it runs the primary-worktree recovery gate described above and records any resulting `stash`, `land`, or `commit` recovery actions in the run payload. Tasks completed during that active run stay visible in place on the execution map until the next run starts and sweeps them away. If there is no runnable or running work after that opening sweep, the command returns `idle` immediately instead of waiting for future tasks. Otherwise the run exits only when it reaches idle or when a `stop` inbox message puts it into draining mode. `stop` prevents new launches but does not interrupt already-running child tasks.

Claimed tasks no longer have a lease timeout. `blackdog claim` can record the long-lived claiming process with `--pid PID`, and supervisor child claims record that pid automatically. A live claimed process can run indefinitely; the supervisor only recovers a claim when the reported claiming pid is missing on repeated liveness scans, and even then it releases the claim instead of killing a still-live task.

`blackdog supervise status` is the chat-native inspection surface for that run. It reports the latest saved run status for a supervisor actor, the resolved WTAM workspace contract, the current pre-launch recovery decision when one exists, the currently open `stop` control messages for that actor, the current ready-task queue, and the most recent supervisor or child-agent task results in one compact text or JSON view.

`blackdog supervise recover` is the structured interruption-recovery surface. It reports recent supervisor runs and child executions, and highlights recoverable cases with suggested follow-up actions. Recoverable dirty-primary rows now include the branch/primary metadata the supervisor uses for the pre-launch recovery gate. Use this command to inspect what the supervisor will try to land, commit, or stash before deciding whether to relaunch, clean up, retry, or complete a replacement flow yourself.

`blackdog supervise report` is the operator metrics surface. It reads historical supervisor events/status/results and summarizes startup friction (launch pressure/failures), retry pressure (task re-run rate), output-shape consistency (expected artifact presence), and landing outcomes (landing failures and success). This report is read-only and intended for quick ergonomics diagnostics across the most recent runs.

`blackdog snapshot` prints the canonical JSON contract embedded into the static `backlog-index.html` page. That payload drives the current board: the `Backlog Control` hero, `Status` counters (running, waiting, blocked, last sweep completed, completed today, completed all-time), active objective-table summaries, the live `Execution Map`, and grouped completed-task history. It includes repo identity (`project_name`, `project_root`, `control_dir`), the current WTAM workspace contract, render headers, hero highlights, `content_updated_at`, the board-facing `last_checked_at`, the raw `supervisor_last_checked_at` heartbeat when available, the latest recorded activity actor/timestamp, backlog counts, push/objective metadata, objective rows with progress summaries, next-focus rows, graph nodes and dependency edges, per-task compute/result/run metadata, stdout-derived model-response excerpts, landed-commit metadata, open inbox messages, direct artifact links, focus-task summaries, recent task-result summaries, release gates, and grouping guidance.

`blackdog render` writes the static `backlog-index.html` page under the configured control root. Blackdog CLI writes and active supervisor runs rerender that page as part of normal state changes, including supervisor exit after landed task-state updates. The page embeds the current snapshot JSON directly, renders a wider control/status top band, a paired objective/release-gates row, the live execution map, and grouped completed-task history, keeps artifact navigation as plain links, shows only active objective rows in the objective table, groups completed history by sweep plus objective, and opens execution/history cards in the task reader. When a child run captured `stdout.log` and a landed commit, the reader also shows the inline model response plus a landed-commit link or message. Operators can still reload the file manually, and the hero header now includes an optional 30-second auto-reload toggle with a visible countdown.

### Structured results

- `blackdog result record --id TASK --actor NAME --status success|blocked|partial ...`

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

- `blackdog-child result record --status ...`
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

`blackdog bootstrap` is now the preferred one-command host-repo entrypoint. `blackdog-skill new backlog` remains as a compatibility wrapper that ensures the project has a Blackdog profile/artifact set and a project-local skill under `.codex/skills/blackdog/`.

`blackdog-skill refresh backlog` regenerates `SKILL.md` and `agents/openai.yaml` from the current `blackdog.toml` profile without rebuilding backlog/runtime files. Use it after changing validation commands, taxonomy, or other repo-local contract details that agents should see.
