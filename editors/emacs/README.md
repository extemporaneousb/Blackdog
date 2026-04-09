# Blackdog Emacs Package

This directory contains the local-use Emacs 30+ package for operating Blackdog inside Emacs.

Current surfaces:

- Magit-style dashboard for backlog state, bucketed task queues, separate completed history, and direct cockpit actions for chat/monitor/stats
- task reader with artifact links and Magit task actions
- Codex-session browser and freeform markdown composer backed by the real Codex CLI, with per-chat model/reasoning controls, live auto-follow, and clickable Blackdog task references in streamed output
- results and supervisor-run listings
- task, artifact, and project-file completion
- repo and artifact grep
- conversation-first task drafting, with legacy spec drafting still available
- telemetry, snapshot stats, and supervisor health

Contract notes:

- shared backlog/runtime reads should come from `blackdog snapshot` via `snapshot.core_export`
- top-level `snapshot.tasks`, `board_tasks`, run metadata, and artifact hrefs remain the UI projection for task-reader and artifact-heavy views
- Codex sessions are the default chat surface; `blackdog-thread.el` stays as a legacy reader/writer for Blackdog-owned prompt/task threads
- the legacy `blackdog-thread.el` and spec-first drafting flow remain transitional surfaces; see [docs/MIGRATION.md](../../docs/MIGRATION.md) and [docs/RELEASE_NOTES.md](../../docs/RELEASE_NOTES.md) for the repo-wide compatibility plan

Minimal local install:

```elisp
(use-package magit :ensure t)
(use-package transient :ensure t)

(use-package blackdog
  :load-path "/Users/bullard/Work/Blackdog/editors/emacs/lisp"
  :bind-keymap (("C-c b" . blackdog-prefix-map)))
```

Run `make test-emacs` from the repo root for batch ERT coverage.
Run `make acceptance` from the repo root for the closeout validation
pass that exercises both the Python and Emacs surfaces.

Use [docs/EMACS.md](../../docs/EMACS.md) for the full architecture, dependency tiers, keybindings, workflows, Magit/worktree behavior, and packaging notes.
