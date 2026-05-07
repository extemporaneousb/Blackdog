---
name: blackdog
description: "Repo-local AI development workflow for Blackdog, backed by Blackdog."
---

# Repo Skill: Blackdog

Use this repo-local skill for normal development requests. The skill is backed by the repo-local Blackdog CLI, but users do not need to name Blackdog workflow primitives in ordinary requests.
`blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.

## User Workflows

- `$blackdog install or update in this repo`: before this repo-local skill exists, analyze the repo, then run `./.VE/bin/blackdog repo install --project-root .` when missing or `./.VE/bin/blackdog repo update --project-root .` followed by `./.VE/bin/blackdog repo refresh --project-root .` when already installed; finish with `git status --short` and commit or land managed repo changes, or report the checkout as intentionally dirty.
- `$blackdog scaffold project <description>`: ask only for missing durable choices such as target path, project name, exemplar repo, validation commands, routed docs, local project access, and app/runtime needs; preview with `./.VE/bin/blackdog repo scaffold --target-root TARGET --like EXEMPLAR --project-name NAME --dry-run`, then apply without adding scaffold logic to the generated project skill.
- `$blackdog do <task-description>`: build a concise execution prompt from the request and routed docs, run `./.VE/bin/blackdog task begin --project-root . --actor AGENT --prompt-file EXECUTION_PROMPT --prompt-mode skill --user-prompt-file USER_PROMPT` without `--workset` or `--task`, make changes only in the returned task workspace, validate, then land with `./.VE/bin/blackdog task land --project-root . --summary "..."`.
- For multi-agent work, use the active Codex thread directly and keep Blackdog focused on the task execution and attempt history it can record through the normal `do` flow.

## Internal CLI Surface

- repo lifecycle: `repo analyze`, `repo bind`, `repo table`, `repo install`, `repo scaffold`, `repo update`, `repo refresh`, `repo archive`, `repo unarchive`, `repo unbind`, `attempts summary`, `attempts table`, `codex coverage`, `codex history`
- task execution: `task begin`, `task show`, `task recover`, `task land`, `task close`, `task cancel`, `task reopen`, `task cleanup`
- status and evidence: `summary`, `snapshot`, `attempts summary`, `attempts table`, `codex coverage`, `codex history`
- abandoned work is canceled by default; use `task reopen` only when it should return to normal execution

## Operator Guardrails

- Do not launch an external browser, use macOS `open`, use `xdg-open`, or run headed Playwright/browser sessions for agent verification unless the user explicitly asks for a user-visible browser; prefer Codex in-app browser tools or headless evidence.
- After `repo install`, `repo update`, or `repo refresh`, run `git status --short`; commit or land managed repo changes, or report the checkout as intentionally dirty before finishing.
- Before finishing implementation work, re-check branch and dirty state. Do not leave uncommitted changes from your work; if committing or landing, make sure the result is on the primary `main` branch unless the user explicitly requested another branch.

## Docs To Review

- `AGENTS.md`
- `docs/INDEX.md`
- `docs/PRODUCT_SPEC.md`
- `docs/ARCHITECTURE.md`
- `docs/TARGET_MODEL.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`
