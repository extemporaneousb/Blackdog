# Blackdog Docs

Blackdog vNext is a machine-native planning and runtime kernel for AI-first
repo work. Humans author repo docs, design intent, approvals, and prompts.
Agents mutate planning and runtime state through typed Blackdog operations and
CLI surfaces.

## Primary Docs

- [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md): supported workflows, v1 target,
  keep/change/defer/remove decisions, and example human/agent stories
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): package boundaries, storage
  ownership, and the minimum shipped product surface
- [docs/TARGET_MODEL.md](docs/TARGET_MODEL.md): the vNext object model and the
  deliberate breaking changes that define it
- [docs/TARGET_MODEL_EXECUTION_PLAN.md](docs/TARGET_MODEL_EXECUTION_PLAN.md):
  the sweep note that records the compatibility-first plan as superseded
- [docs/CLI.md](docs/CLI.md): current command surface for `blackdog`
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical schema for
  `planning.json`, `runtime.json`, and `events.jsonl`

## Current Product Surface

- `blackdog workset put`: create or update one durable workset plus optional
  task runtime rows
- `blackdog summary`: read human-oriented status from the typed runtime model
- `blackdog next`: list ready tasks from the task DAG
- `blackdog snapshot`: emit the machine-readable runtime snapshot

## Direction

- Do not author planning truth in markdown.
- Do not treat `epic`, `lane`, or `wave` as durable concepts.
- Do not use architecture prose as the product workflow spec; use
  [docs/PRODUCT_SPEC.md](docs/PRODUCT_SPEC.md) for that.
- Do not depend on removed compatibility surfaces unless the docs explicitly
  reintroduce them.
