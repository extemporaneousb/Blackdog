# Blackdog Docs

Use this index as the entrypoint for the repo-versioned backlog system.

## Document Map

- [docs/ARCHITECTURE.md](/Users/bullard/Work/Blackdog/docs/ARCHITECTURE.md): system boundaries, data flow, and why Blackdog is repo-versioned
- [docs/CLI.md](/Users/bullard/Work/Blackdog/docs/CLI.md): command reference for `blackdog` and `blackdog-skill`
- [docs/FILE_FORMATS.md](/Users/bullard/Work/Blackdog/docs/FILE_FORMATS.md): canonical schema for `blackdog.toml`, backlog markdown, state, events, inbox, and task-result files

## Working Guidance

- Use the CLI for durable state transitions.
- Keep `.blackdog/` checked into the repo when it is part of the active working contract.
- Use `blackdog-skill new backlog` to scaffold project-local skills that point at the repo’s own backlog runtime.

