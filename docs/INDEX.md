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

## Historical / Reference Notes

- [docs/TARGET_MODEL_EXECUTION_PLAN.md](docs/TARGET_MODEL_EXECUTION_PLAN.md):
  superseded rewrite note retained for provenance
- [docs/SINGLE_AGENT_AUDIT.md](docs/SINGLE_AGENT_AUDIT.md): dated April 2026
  WTAM audit kept as non-routed operator reference, not as the live contract
- [docs/REPO_LIFECYCLE_MVP.md](docs/REPO_LIFECYCLE_MVP.md): historical repo
  lifecycle planning note retained as background for the shipped v3 surface

## Current Product Surface

- `blackdog init`
- `blackdog repo analyze`
- `blackdog repo install`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog workset put`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task recover`
- `blackdog task land`
- `blackdog task close`
- `blackdog task cancel`
- `blackdog task reopen`
- `blackdog task cleanup`
- `blackdog summary`
- `blackdog next --workset`
- `blackdog snapshot`
- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`

The shipped surface is intentionally partitioned: `repo`/`prompt`/`attempts`
own repo lifecycle and operator-read workflows, `workset`/`summary`/`next` own
planning and inspection, `task` is the default same-thread WTAM path, and
`worktree` is the explicit planned-task WTAM path.

## Direction

- Do not author planning truth in markdown.
- Do not treat `epic`, `lane`, or `wave` as durable concepts.
- Do not preserve deleted backlog/board/bootstrap/inbox/render/multi-agent
  surfaces on the new typed model.
- Do not use architecture prose as the product workflow spec; use
  [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) for that.
