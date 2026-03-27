# Blackdog Emacs Package

This directory contains the local-use Emacs 30+ package for operating Blackdog inside Emacs.

Current surfaces:

- Magit-style dashboard for backlog state and recent results
- task reader with artifact links and Magit task actions
- results and supervisor-run listings
- task, artifact, and project-file completion
- repo and artifact grep
- spec-first task drafting
- telemetry and supervisor health

Minimal local install:

```elisp
(use-package magit :ensure t)
(use-package transient :ensure t)

(use-package blackdog
  :load-path "/Users/bullard/Work/Blackdog/editors/emacs/lisp"
  :bind-keymap (("C-c b" . blackdog-prefix-map)))
```

Run `make test-emacs` from the repo root for batch ERT coverage.

Use [docs/EMACS.md](../../docs/EMACS.md) for the full architecture, dependency tiers, keybindings, workflows, Magit/worktree behavior, and packaging notes.
