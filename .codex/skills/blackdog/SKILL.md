---
name: blackdog
description: "Repo-local AI development workflow for Blackdog, backed by Blackdog."
---

# Repo Skill: Blackdog

Use this repo-local skill for normal development requests. `AGENTS.md` owns the detailed workflow contract; do not duplicate it here. `blackdog.toml` owns handler setup, validation, and a document-routing catalog. Read only catalog entries relevant to the current task; do not load every routed document by default.

## Workflow

- `$blackdog install or update in this repo`: before this repo-local skill exists, analyze the repo, then run `./.VE/bin/blackdog repo install --project-root .` when missing or `./.VE/bin/blackdog repo update --project-root .` followed by `./.VE/bin/blackdog repo refresh --project-root .` when already installed; finish with `git status --short` and commit or land managed repo changes, or report the checkout as intentionally dirty.
- `$blackdog scaffold project <description>`: ask only for missing durable choices such as target path, project name, exemplar repo, validation commands, routed docs, local project access, and app/runtime needs; preview with `./.VE/bin/blackdog repo scaffold --target-root TARGET --like EXEMPLAR --project-name NAME --dry-run`, then apply without adding scaffold logic to the generated project skill.
- `$blackdog do <task-description>`: create the two mode-0600 UTF-8 temporary prompt files required by `AGENTS.md`; keep the exact triggering request in `request_file` and a concise goal, relevant context, constraints, and done condition in `execution_prompt_file`. Run `./.VE/bin/blackdog task begin --project-root . --actor codex --execution-prompt-file "$execution_prompt_file" --prompt-mode skill --request-file "$request_file" --json` directly. Delete `request_file` and `execution_prompt_file` only when the structured `task begin` result contains both a nonempty `execution_prompt_replay_artifact_path` and a nonempty `user_prompt_replay_artifact_path`; otherwise preserve both temporary inputs.
- Make implementation changes only in the returned task workspace.
- For every structured result from `task begin`, `task show`, `task recover`, `task cancel`, `task reopen`, `task land`, `task reconcile-landing`, `task close`, and `task cleanup`, treat its `next_action` as the sole authority regardless of `operation_status`: execute its exact `argv` when `kind=command`; choose only a complete action from `choices` or `alternatives`; stop when `kind=blocked` or `kind=complete`; never infer an action from display text, reason or error prose, summaries, or compatibility recommendations.
- Validate as required by `AGENTS.md`, then land with `./.VE/bin/blackdog task land --project-root . --summary "$completion_summary" "${validation_args[@]}" --json` using real `NAME=passed|failed|skipped` evidence and a concise human-readable summary.
