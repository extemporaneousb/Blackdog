# Blackdog

Blackdog is a repo-versioned backlog system for AI-assisted software work.

It keeps backlog semantics, state transitions, event history, structured task results, inbox messages, and HTML status views inside the project repo instead of hiding them inside global skills or ad hoc scripts.

## What it provides

- `blackdog`: core CLI for backlog parsing, validation, task selection, claims, approvals, completion, inbox messaging, task results, and HTML rendering
- `blackdog-skill`: project scaffold and project-specific skill generator
- `blackdog.toml`: repo-local profile for id prefixes, bucket/domain taxonomy, defaults, and heuristics
- `.blackdog/`: repo-local backlog artifacts

## Quick start

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e .
blackdog init --project-name "My Project"
blackdog add \
  --title "Create first task" \
  --bucket core \
  --why "We need a real task in the queue." \
  --evidence "The backlog starts empty." \
  --safe-first-slice "Add one narrowly scoped task with a lane and render the HTML view."
blackdog summary
blackdog render
blackdog-skill new backlog --project-root .
```

## Design goals

- Keep the backlog runtime versioned with the repo that depends on it.
- Make skills thin adapters around a real CLI and real file formats.
- Preserve human-readable backlog markdown while moving execution semantics into structured state and event files.
- Support AI agents with explicit claims, messages, structured results, and predictable file layouts.

See [docs/INDEX.md](/Users/bullard/Work/Blackdog/docs/INDEX.md) for the full document map.

