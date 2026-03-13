# Architecture

Blackdog is a local-first backlog runtime for AI-assisted software work.

## Core idea

The backlog system should live in the repo that depends on it. Skills should explain how to use it, but they should not be the source of executable state logic.

## Main layers

1. `blackdog.toml`
   - Repo-local profile.
   - Defines id prefix, bucket/domain taxonomy, defaults, and artifact paths.

2. `src/blackdog/`
   - Core runtime.
   - Owns backlog parsing, validation, selection, state transitions, events, inbox messages, structured results, and HTML view generation.

3. `.blackdog/`
   - Repo-local artifact set.
   - Holds `backlog.md`, `backlog-state.json`, `events.jsonl`, `inbox.jsonl`, `task-results/`, and `backlog-index.html`.

4. Project-local skill scaffold
   - Generated under `.codex/skills/blackdog-backlog/`.
   - Tells an AI agent how to use the local CLI and local artifact paths.

## Why this split

- It avoids version skew between the repo and globally installed skill logic.
- It keeps stateful behavior testable.
- It preserves human-readable backlog markdown while moving execution state into structured files.
- It gives AI agents a durable message channel and structured task-result channel.

## Runtime model

1. `blackdog init` creates the profile and artifact set.
2. `blackdog add` appends backlog tasks and updates the plan block.
3. `blackdog claim`, `release`, `complete`, and `decide` update `backlog-state.json` and append `events.jsonl`.
4. `blackdog inbox ...` manages directed messages between user, supervisor, and child agents.
5. `blackdog result record` writes a task-result JSON file and appends an event.
6. `blackdog render` rebuilds the HTML control page from the current backlog, state, inbox, events, and task results.

