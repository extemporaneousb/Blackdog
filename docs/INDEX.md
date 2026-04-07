# Blackdog Docs

Use this index as the entrypoint for the repo-scoped Blackdog system.

## Document Map

- [docs/CHARTER.md](docs/CHARTER.md): product intent, current-vs-target scope, and success criteria for the multi-agent backlog system
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system boundaries, data flow, and the split between repo-local contract and shared runtime state
- [docs/MODULE_INVENTORY.md](docs/MODULE_INVENTORY.md): file-level ownership tags and extraction targets across core runtime, Blackdog proper, adapters, and removal candidates
- [docs/CLI.md](docs/CLI.md): command reference for `blackdog`, including `blackdog bootstrap`, and the `blackdog-skill` compatibility wrapper
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical schema for `blackdog.toml`, backlog markdown, state, events, inbox, and task-result files
- [docs/INTEGRATION.md](docs/INTEGRATION.md): current host-repo setup flow, configuration review points, and pilot rollout guidance
- [docs/EXTRACTION_AUDIT.md](docs/EXTRACTION_AUDIT.md): extraction-risk audit for viewer, scaffold, supervisor, and Emacs adapter surfaces

## Working Guidance

- Use the CLI for durable state transitions.
- Use `blackdog worktree ...` for implementation tasks instead of editing from the primary checkout.
- Do not plan around checked-in mutable runtime artifacts.
- Use `blackdog bootstrap` to scaffold a host repo into the Blackdog contract in one command.

Current direction:
- Treat the shared git control root as the runtime contract and `blackdog.toml` as the repo-local entrypoint.
