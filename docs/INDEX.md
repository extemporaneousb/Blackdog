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
- `events.jsonl` is append-only lifecycle and outcome evidence.
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
blackdog repo analyze|bind|table|install|scaffold|update|refresh|migrate|archive|unarchive|unbind
blackdog task begin|show|recover|cancel|reopen|land|reconcile-landing|close|cleanup|outcome|validate
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
- [Execution and outcome plan](EXECUTION_OUTCOMES_PLAN.md) — implemented core
  decisions, worker sequence, and A1-A9 acceptance evidence.
- [Release acceptance](RELEASE_ACCEPTANCE.md) — final workload measurements,
  canonical implementation commits, review proof, and delivery limits.
- [Runtime distribution](RUNTIME_DISTRIBUTION.md) — reproducible executable
  archives, installation, upgrades, and optional project environments.
- [Runtime and worktree preparation contract](WORKTREE_PREPARATION.md) — accepted
  isolation boundary, declared preparation requirements, current gaps and staged proof.
- [Runtime acceptance](RUNTIME_ACCEPTANCE.md) — isolated external lifecycle
  checks and comparable measurement conditions.
- [Outcome evidence](OUTCOME_EVIDENCE.md) — typed criteria, validation, measurement and reports.

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

## Runtime Distribution

Blackdog ships a reproducible standalone Python archive and needs no repository
or task `.VE` to run. Git and system Python 3.11 or newer are required. The
[distribution runbook](RUNTIME_DISTRIBUTION.md) describes immutable control-root
snapshots, cleanup-safe recovery, deliberate source execution for Blackdog
development, and preservation of explicit project environment handlers.

Bundling an interpreter or moving to a native language remains deferred pending
packaging and performance evidence. Distribution changes do not replace the
canonical task store or its migration protocol.
