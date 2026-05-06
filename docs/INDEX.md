# Blackdog Docs

Blackdog vNext is a machine-native planning and runtime kernel for AI-first
repo work. Humans author repo docs, design intent, approvals, and prompts.
Agents mutate planning and runtime state through typed Blackdog operations and
CLI surfaces.

## Primary Docs

- [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md): supported workflows, v1 target,
  and keep/change/defer/remove decisions
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): package boundaries, storage
  ownership, and the current shipped product surface
- [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md): the vNext object model and the
  deliberate breaking changes that define it
- [docs/CLI.md](docs/CLI.md): current command surface for `blackdog`
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical schema for
  `planning.json`, `runtime.json`, and `events.jsonl`

## Current Product Surface

- `blackdog init`
- `blackdog repo analyze`
- `blackdog repo bind`
- `blackdog repo table`
- `blackdog repo install`
- `blackdog repo scaffold`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog repo archive`
- `blackdog repo unarchive`
- `blackdog repo unbind`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog codex coverage`
- `blackdog codex history`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task recover`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cancel`
- `blackdog task reopen`
- `blackdog task cleanup`
- `blackdog summary`
- `blackdog snapshot`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`

The shipped surface is intentionally partitioned: `repo`/`prompt`/`attempts`
own repo lifecycle and operator-read workflows, `codex` owns Codex-session
coverage/history indexing, `summary`/`snapshot` expose task-first current
state, `task` is the default same-thread WTAM path, and `worktree` is the
explicit planned-task WTAM path. Direct workset reads remain a migration/debug
concern, not the default operator surface.

## Direction

- Do not author planning truth in markdown.
- Do not treat `epic`, `lane`, or `wave` as durable concepts.
- Do not preserve deleted backlog/board/bootstrap/inbox/render/multi-agent
  surfaces on the new typed model.
- Do not use architecture prose as the product workflow spec; use
  [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) for that.
