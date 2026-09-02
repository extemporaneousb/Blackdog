# Blackdog Documentation

Blackdog is a deterministic local task protocol for AI-driven development. It
standardizes task ownership, attempt provenance, isolated workspaces, recovery,
validation, landing, and cleanup so an agent can concentrate on the requested
engineering outcome.

## Product Contract

- A task is one executable intent with repository-unique identity.
- An attempt is one execution of that task by one actor.
- At most one attempt is active for a task.
- `runtime.json` is the only canonical mutable task store.
- `events.jsonl` is append-only lifecycle evidence.
- Request and execution prompts are content-addressed private artifacts.
- Structured lifecycle results expose exactly one authoritative `next_action`.
- Repository policy stays in `blackdog.toml`; the protocol stays in Blackdog.
- Provider conversations remain provider-owned. Blackdog records bounded
  references and task-level evidence, not transcripts.

## Shipped Commands

```text
blackdog init
blackdog summary
blackdog snapshot
blackdog stats
blackdog local-repo add|list|remove
blackdog prompt preview
blackdog attempts summary|table
blackdog codex coverage|history|hook stamp
blackdog repo analyze|bind|table|install|scaffold|update|refresh|archive|unarchive|unbind
blackdog task begin|show|recover|cancel|reopen|land|reconcile-landing|close|cleanup
blackdog worktree preflight|table
```

`task begin` is the normal implementation entrypoint. `worktree preflight` and
`worktree table` are read-only diagnosis. Product code owns all mutation; agents
must never edit control files directly.

## Documents

- [Architecture](ARCHITECTURE.md) — boundaries, state model, lifecycle, and
  recovery guarantees.
- [CLI](CLI.md) — command and structured-result contract.
- [File formats](FILE_FORMATS.md) — canonical files and durable schemas.

`AGENTS.md` contains the repository workflow and the generated contract copied
into managed repositories.

## Scope Boundaries

The current core executes directly identified tasks and records their attempts.
It does not include:

- A background coordination service or workflow language
- Automatic task decomposition, ordering, or subsequent-task selection
- Online prompt optimization or self-modification
- Transcript storage
- A provider-specific agent runtime
- A dashboard or board
- Readers for superseded task-store formats

Future task relationships must connect executable tasks directly. They must not
introduce another durable planning object or hidden policy inheritance.

## Deferred Runtime Distribution

This contract-removal phase intentionally leaves repository-local `.VE` setup
unchanged. The next stages are:

1. Decouple Blackdog's executable from target-repository environments.
2. Test self-contained Python release artifacts built by CI.
3. Consider a native-language port only if packaging evidence shows that the
   self-contained Python runtime is insufficient.

No packaging or runtime-distribution behavior is part of the current durable
task contract.
