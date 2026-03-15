# CLI Reference

The current CLI covers the backlog runtime, a one-shot supervisor runner, an initial long-lived supervisor loop, and a static task index renderer.

When a repo keeps Blackdog in a repo-local virtual environment, prefer that entrypoint (for example `./.VE/bin/blackdog`) over a different `blackdog` on `PATH`.

## `blackdog`

### Project setup

- `blackdog bootstrap --project-root PATH --project-name NAME`
- `blackdog init --project-root PATH --project-name NAME`
- `blackdog validate`
- `blackdog render`
- `blackdog snapshot`
- `blackdog worktree preflight`
- `blackdog worktree start --id TASK`
- `blackdog worktree land [--branch BRANCH] [--into TARGET]`
- `blackdog worktree cleanup --id TASK|--path PATH`

Use `blackdog bootstrap` for normal host-repo adoption. Use `blackdog init` only when you want the repo-local artifact set without generating the project-local skill scaffold.

`blackdog worktree ...` is the implementation-work entrypoint. WTAM is the implementation model:

- implementation work should happen from a branch-backed task worktree, not the primary checkout
- `blackdog worktree start` creates a task branch from the primary worktree branch and returns a structured worktree spec
- `blackdog worktree land` fast-forwards that task branch into the target branch and can remove the task worktree with `--cleanup`
- `blackdog worktree cleanup` removes a landed task worktree and, when explicitly told, deletes the associated branch
- `blackdog worktree preflight` reports the central project root, the actual current `cwd` and worktree, the primary worktree, configured worktree base, whether there are implementation-blocking local changes, the enforced WTAM workspace contract, the target branch, primary-worktree landing cleanliness, and the per-worktree `.VE` rule/CLI path for the current checkout

For Blackdog's own repo, manual-first is the default operating mode
until the runtime-hardening tasks land. Prefer the direct `blackdog
claim` -> `blackdog worktree preflight|start` -> `blackdog result
record` -> land/`blackdog complete` flow for normal Blackdog-on-
Blackdog work. Use `blackdog supervise run|loop` when you are
explicitly exercising delegated execution or supervisor behavior, not
as the required path to continue product development.

### Backlog management

- `blackdog backlog new NAME`
- `blackdog backlog remove NAME`
- `blackdog backlog reset`
- `blackdog add --title ... --bucket ... --why ... --evidence ... --safe-first-slice ...`
- `blackdog plan`
- `blackdog summary`
- `blackdog next`
- `blackdog supervise run`
- `blackdog supervise loop`
- `blackdog supervise status`
- `blackdog claim --agent NAME`
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

Blackdog creates branch-backed child worktrees from the primary worktree branch and treats committed repo state as the delegated baseline. If landing is blocked by dirty primary-worktree changes, the supervisor treats that as a contract violation: it sends an inbox warning, records a blocked supervisor result, and leaves the child branch/worktree in place for inspection. Child agents are expected to commit on their task branch, and the supervisor lands that branch through the primary worktree with fast-forward semantics before completing the task.

The generated child prompt tells the agent that committed repo state is the baseline, that the task is already claimed by the supervisor, that it must commit changes on the task branch, and that Blackdog CLI output should be treated as the source of truth for backlog state. It also surfaces the run workspace mode, the task branch to target-branch landing path, the primary-worktree cleanliness gate, and the per-worktree `.VE` rule. When the current workspace contains `.VE/bin/blackdog`, the prompt points child agents at that workspace-local CLI; otherwise it falls back to `blackdog` from the active environment and tells the agent to bootstrap `./.VE` in that worktree rather than reusing another worktree's environment.

`blackdog supervise loop` keeps a supervisor session alive across multiple cycles, writes loop status under the configured control root `supervisor-runs/` directory, refreshes the HTML control page after each cycle, and can be steered through inbox messages sent to the supervisor actor. `pause` messages prevent new launches while they remain open, and `stop` messages end the loop before the next cycle starts. These are boundary controls; they do not interrupt a child task that is already running.

`blackdog supervise status` is the chat-native inspection surface for that loop. It reports the latest saved loop status for a supervisor actor, the currently open `pause`/`stop` control messages for that actor, the current ready-task queue, the most recent supervisor or child-agent task results, and the resolved WTAM workspace contract for that actor in one compact text or JSON view.

`blackdog snapshot` prints the canonical JSON contract embedded into the static `backlog-index.html` page. It includes repo identity (`project_name`, `project_root`, `control_dir`), the current WTAM workspace contract, the latest recorded activity actor/timestamp, backlog counts, objectives, graph nodes and dependency edges, per-task compute/result metadata, operator-facing task status chips, direct artifact links, active-task summaries, recent task results, and grouping guidance.

`blackdog render` writes the static `backlog-index.html` page under the configured control root. Blackdog CLI writes and supervisor loop cycles rerender that page as part of normal state changes. The page embeds the current snapshot JSON directly, renders a full-width lane board with clickable status counters, keeps the inbox compact in the top-right corner, and links to filesystem artifacts like result JSON files, prompt/stdout/stderr logs, metadata, and captured child diffs. Reload the file when you want the latest state.

### Structured results

- `blackdog result record --id TASK --actor NAME --status success|blocked|partial ...`

### Inbox

- `blackdog inbox send --sender NAME --recipient NAME --body ...`
- `blackdog inbox list`
- `blackdog inbox resolve --message-id ID --actor NAME`

## `blackdog-skill`

- `blackdog-skill new backlog --project-root PATH`
- `blackdog-skill refresh backlog --project-root PATH`

`blackdog bootstrap` is now the preferred one-command host-repo entrypoint. `blackdog-skill new backlog` remains as a compatibility wrapper that ensures the project has a Blackdog profile/artifact set and a project-local skill under `.codex/skills/blackdog/`.

`blackdog-skill refresh backlog` regenerates `SKILL.md` and `agents/openai.yaml` from the current `blackdog.toml` profile without rebuilding backlog/runtime files. Use it after changing validation commands, taxonomy, or other repo-local contract details that agents should see.
