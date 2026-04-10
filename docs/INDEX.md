# Blackdog Docs

Use this index as the entrypoint for the repo-scoped Blackdog system.

## Document Map

- [docs/CHARTER.md](docs/CHARTER.md): product intent, current-vs-target scope, and success criteria for the multi-agent backlog system
- [docs/BOUNDARIES.md](docs/BOUNDARIES.md): frozen ownership split between `core`, `blackdog`, and optional `extensions`
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): system boundaries, data flow, and the split between repo-local contract and shared runtime state
- [docs/architecture-diagrams.html](docs/architecture-diagrams.html): generated maintainer overview rendered from the checked-out code, including workflows, module/class summaries, and the full CLI inventory
- [docs/MODULE_INVENTORY.md](docs/MODULE_INVENTORY.md): file-level ownership tags and extraction targets across core runtime, `blackdog`, adapters, and removal candidates
- [docs/CLI.md](docs/CLI.md): command reference for the `blackdog` executable and the `blackdog_cli` adapter package
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical schema for `blackdog.toml`, backlog markdown, state, events, inbox, and task-result files
- [docs/INTEGRATION.md](docs/INTEGRATION.md): current host-repo setup flow, configuration review points, and pilot rollout guidance
- [docs/MIGRATION.md](docs/MIGRATION.md): migration guidance for callers moving onto the final remodeled surface
- [docs/RELEASE_NOTES.md](docs/RELEASE_NOTES.md): final remodel release notes and compatibility summary
- [docs/ACCEPTANCE.md](docs/ACCEPTANCE.md): final acceptance checklist, evidence sources, and validation commands
- [docs/EXTRACTION_AUDIT.md](docs/EXTRACTION_AUDIT.md): extraction-risk audit for viewer, scaffold, supervisor, and Emacs adapter surfaces
- [docs/OWNERSHIP_INVENTORY.md](docs/OWNERSHIP_INVENTORY.md): current file-level ownership map across `blackdog_core`, `blackdog`, viewers, and clients for extraction planning

## Working Guidance

- Use the CLI for durable state transitions.
- Use `blackdog worktree ...` for implementation tasks instead of editing from the primary checkout.
- Do not plan around checked-in mutable runtime artifacts.
- Use `blackdog bootstrap` to scaffold a host repo into the Blackdog contract in one command.

Current direction:
- Treat the shared git control root as the runtime contract and `blackdog.toml` as the repo-local entrypoint.
- Treat `docs/BOUNDARIES.md` as the ownership contract for what belongs in `core`, `blackdog`, and `extensions`.
