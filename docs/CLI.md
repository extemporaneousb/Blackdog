# CLI Reference

The current CLI covers the backlog runtime, a one-shot supervisor runner, an initial long-lived supervisor loop, and a served readonly live UI.

## `blackdog`

### Project setup

- `blackdog bootstrap --project-root PATH --project-name NAME`
- `blackdog init --project-root PATH --project-name NAME`
- `blackdog validate`
- `blackdog render`
- `blackdog ui snapshot`
- `blackdog ui serve`
- `blackdog worktree preflight`
- `blackdog worktree start --id TASK`
- `blackdog worktree land [--branch BRANCH] [--into TARGET]`
- `blackdog worktree cleanup --id TASK|--path PATH`

Use `blackdog bootstrap` for normal host-repo adoption. Use `blackdog init` only when you want the repo-local artifact set without generating the project-local skill scaffold.

`blackdog worktree ...` is the implementation-work entrypoint. The intended model now matches WTAM more closely:

- implementation work should happen from a branch-backed task worktree, not the primary checkout
- `blackdog worktree start` creates a task branch from the primary worktree branch and returns a structured worktree spec
- `blackdog worktree land` fast-forwards that task branch into the target branch and can remove the task worktree with `--cleanup`
- `blackdog worktree cleanup` removes a landed task worktree and, when explicitly told, deletes the associated branch
- `blackdog worktree preflight` reports the current worktree, primary worktree, configured worktree base, and whether there are implementation-blocking local changes

### Backlog management

- `blackdog add --title ... --bucket ... --why ... --evidence ... --safe-first-slice ...`
- `blackdog plan`
- `blackdog summary`
- `blackdog next`
- `blackdog supervise run`
- `blackdog supervise loop`
- `blackdog claim --agent NAME`
- `blackdog release --id TASK --agent NAME`
- `blackdog complete --id TASK --agent NAME`
- `blackdog decide --id TASK --agent NAME --decision approved|denied|deferred|done`
- `blackdog comment --actor NAME --id TASK --body ...`
- `blackdog events`

`blackdog supervise run` now assumes an exec-capable Codex launcher. With the default config, it prefers the Codex.app runtime when available and no longer supports the legacy prompt launcher.

The default `worktrees_dir` is now `../.worktrees`, which keeps Blackdog task worktrees as siblings of the primary checkout rather than nesting them under repo-controlled runtime artifacts. If a repo prefers a `.worktrees` symlink inside the repo root, Blackdog will follow that resolved path when it is configured in `blackdog.toml`.

When `workspace_mode = "git-worktree"`, Blackdog creates a branch-backed child worktree from the primary worktree branch. The primary worktree must be clean with respect to implementation changes before launch. Child agents are expected to commit on their task branch, and the supervisor lands that branch through the primary worktree with fast-forward semantics before completing the task.

The generated child prompt tells the agent that committed repo state is the baseline, that the task is already claimed by the supervisor, that it must commit changes on the task branch, and that Blackdog CLI output should be treated as the source of truth for backlog state.

`blackdog supervise loop` keeps a supervisor session alive across multiple cycles, writes loop status under the configured control root `supervisor-runs/` directory, refreshes the HTML control page after each cycle, and can be steered through inbox messages sent to the supervisor actor. `pause` messages prevent new launches while they remain open, and `stop` messages end the loop before the next cycle starts. These are boundary controls; they do not interrupt a child task that is already running.

`blackdog ui snapshot` prints the canonical JSON contract used by the live UI. It includes backlog counts, objectives, graph nodes and dependency edges, open inbox messages, recent task results, recent supervisor runs, and recent supervisor loops.

`blackdog ui serve` starts a local HTTP server that serves a readonly monitor over the same snapshot contract. The UI shell lives at `/`, the full snapshot is exposed at `/api/snapshot`, and server-sent events are streamed from `/api/stream`. Blackdog write paths notify that server on state changes, so the browser updates without polling. The server also exposes repo-local runtime artifacts under `/artifacts/...`.

### Structured results

- `blackdog result record --id TASK --actor NAME --status success|blocked|partial ...`

### Inbox

- `blackdog inbox send --sender NAME --recipient NAME --body ...`
- `blackdog inbox list`
- `blackdog inbox resolve --message-id ID --actor NAME`

## `blackdog-skill`

- `blackdog-skill new backlog --project-root PATH`
- `blackdog-skill refresh backlog --project-root PATH`

`blackdog bootstrap` is now the preferred one-command host-repo entrypoint. `blackdog-skill new backlog` remains as a compatibility wrapper that ensures the project has a Blackdog profile/artifact set and a project-local skill under `.codex/skills/blackdog-backlog/`.

`blackdog-skill refresh backlog` regenerates `SKILL.md` and `agents/openai.yaml` from the current `blackdog.toml` profile without rebuilding backlog/runtime files. Use it after changing validation commands, taxonomy, or other repo-local contract details that agents should see.
