# Blackdog Docs

Blackdog is a machine-native task and attempt runtime for AI-driven local
development. Keep the docs small and contract-oriented: each file below owns a
distinct question.

## Source Of Truth

- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md): package boundaries, product
  layers, repo install/layering, and shipped workflow ownership.
- [docs/CLI.md](docs/CLI.md): current command surface for `blackdog`.
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md): canonical schemas for
  `blackdog.toml`, `planning.json`, `runtime.json`, `events.jsonl`, history
  rows, optional lifecycle observations, and managed repo artifacts.

## Current Product Surface

- `blackdog init`
- `blackdog summary`
- `blackdog snapshot`
- `blackdog stats`
- `blackdog local-repo add`
- `blackdog local-repo list`
- `blackdog local-repo remove`
- `blackdog prompt preview`
- `blackdog prompt tune`
- `blackdog attempts summary`
- `blackdog attempts table`
- `blackdog codex link`
- `blackdog codex coverage`
- `blackdog codex history`
- `blackdog codex hook stamp`
- `blackdog repo install`
- `blackdog repo bind`
- `blackdog repo table`
- `blackdog repo archive`
- `blackdog repo unarchive`
- `blackdog repo unbind`
- `blackdog repo analyze`
- `blackdog repo scaffold`
- `blackdog repo update`
- `blackdog repo refresh`
- `blackdog task begin`
- `blackdog task show`
- `blackdog task recover`
- `blackdog task cancel`
- `blackdog task reopen`
- `blackdog task land`
- `blackdog task reconcile-landing`
- `blackdog task close`
- `blackdog task cleanup`
- `blackdog worktree preflight`
- `blackdog worktree table`
- `blackdog worktree preview`
- `blackdog worktree start`
- `blackdog worktree show`
- `blackdog worktree land`
- `blackdog worktree close`
- `blackdog worktree cleanup`

The shipped surface is intentionally partitioned: `repo`/`prompt`/`attempts`
own repo lifecycle and operator-read workflows, `local-repo` owns user-local
registry management, `codex` owns task-worktree links, Codex-session
coverage/history indexing, and hook-backed task-context observability,
`stats` owns cross-repo metrics, `summary`/`snapshot` expose task-first current
state, `task` is the default same-thread WTAM path, and `worktree` is the
explicit planned-task WTAM path.

## Direction

- Do not author planning truth in markdown.
- Do not use architecture prose as an alternate CLI or schema contract.
- Do not preserve deleted backlog, board, bootstrap, inbox, render, or
  multi-agent runtime surfaces on the typed model.
- Keep repository policy in optional, repo-owned `[[guards]]` configuration;
  Blackdog supplies only the generic execution and evidence contract.
- Keep generated repo skills thin: route work through the Blackdog CLI and
  record durable attempts instead of encoding workflow logic in prompt prose.
  Treat `doc_routing_defaults` as a catalog: agents select only the entries
  relevant to the current request instead of loading the full list by default.
- Validate repo installation and layering through the normal test suite and
  operator-facing `repo analyze`/`worktree preflight` checks.

## Research And Case Studies

These documents are non-authoritative design inputs. They describe measured
history and proposals, not shipped commands or file formats:

- [AdaptivePlotter sequential execution case study](docs/research/ADAPTIVEPLOTTER_SEQUENTIAL_EXECUTION_CASE_STUDY.md)
- [Multi-agent sequential-execution research prompt](docs/research/MULTI_AGENT_SEQUENTIAL_EXECUTION_RESEARCH_PROMPT.md)
