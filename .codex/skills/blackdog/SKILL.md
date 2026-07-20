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
- `$blackdog do <task-description>`: materialize two mode-0600 UTF-8 temporary files outside the repo. Set `request_file` to the absolute path containing the exact triggering user request verbatim; set `execution_prompt_file` to the absolute path containing the concise goal, context, constraints, and done condition prompt composed from that request and routed docs. Run `./.VE/bin/blackdog task begin --project-root . --actor codex --execution-prompt-file "$execution_prompt_file" --prompt-mode skill --request-file "$request_file" --json` directly without `--workset` or `--task`. Delete `request_file` and `execution_prompt_file` only when the structured `task begin` result contains both a nonempty `execution_prompt_replay_artifact_path` and a nonempty `user_prompt_replay_artifact_path`; otherwise preserve both temporary inputs. `task begin` performs its own readiness checks, so a separate preflight is not required; make changes only in the returned task workspace; validate; set `completion_summary` to the actual summary and build `validation_args` with at least one repeated `--validation` and `NAME=passed|failed|skipped`; then run `./.VE/bin/blackdog task land --project-root . --summary "$completion_summary" "${validation_args[@]}" --json`. Never submit placeholders or invented validation.
- `task begin --actor` defaults to the stable owner `codex`; the generated command supplies it explicitly. For supervised multi-agent work, the one supervisor may instead use `codex-supervisor` and remains the sole Blackdog task/attempt owner. Workers do not run `task begin`, create parallel Blackdog attempts, or land separately; their contributions and reviews remain inside the supervisor-owned task.

## Execution Contract

- Inputs: the user's task request, routed docs from `blackdog.toml`, and the repo-local managed AGENTS contract.
- Output: one landed commit with Blackdog trailers, or an explicit `task close` result with status, summary, residuals, and follow-ups.
- For every structured result from `task begin`, `task show`, `task recover`, `task cancel`, `task reopen`, `task land`, `task reconcile-landing`, `task close`, and `task cleanup`, treat its `next_action` as the sole authority regardless of `operation_status`: execute its exact `argv` when `kind=command`; choose only a complete action from `choices` or `alternatives`; stop when `kind=blocked` or `kind=complete`; never infer an action from display text, reason or error prose, summaries, or compatibility recommendations.
- Do not manually invent or switch Codex session references. Normal `task begin` captures the invoking turn as best-effort evidence; capture missingness never blocks work, and `codex coverage`/`codex history` are the reconciliation surfaces.
- Use `blackdog codex link` only for an explicit continuation into a new Codex local chat. It targets the active Blackdog task worktree without transferring branch, landing, or cleanup ownership to Codex.
- Retained-workspace adoption and adoption-completion repair are internal recovery routes emitted by `task show`/`task recover`. Execute only their exact `next_action.argv`; never invent adoption flags, expected-value guards, rebase targets, reconciliation commits, cleanup, or a replacement task.
- Direct read-only `task show`, read-only `task recover`, and CLI `worktree show` may detect one legacy landing candidate within 64 target first-parent commits after the exact attempt start. Execute only the emitted read-only dry-run `next_action.argv`; never add `--apply` unless that proof command returns its guarded apply action. Internal/mutation/table/stats reads do not run this scan.
- If any task surface reports `next_action.action_id=retry_stale_claim_release_finalization`, execute that exact owner-task argv before claim-mutating begin/land/close or owner cancel/reopen. Its hidden request/decision guards are machine-emitted replay capabilities; never invent, edit, remove, or reuse them. Stop if Blackdog returns a blocked or conflict action.
- If any task surface reports `next_action.action_id=retry_task_close_finalization`, execute that exact argv until close completes. Its hidden close-request guard and terminal evidence are machine-emitted replay capabilities; never omit, edit, or reconstruct them. A blocked action has no recovery command and requires evidence inspection.
- Keep this skill thin: delegate setup, state, recovery, and landing to the Blackdog CLI rather than encoding workflow state in prompt prose.

## Internal CLI Surface

- project initialization, status, and fleet reporting: `blackdog init`, `blackdog summary`, `blackdog snapshot`, `blackdog stats`
- local registry: `blackdog local-repo add`, `blackdog local-repo list`, `blackdog local-repo remove`
- prompt composition: `blackdog prompt preview`, `blackdog prompt tune`
- attempt evidence: `blackdog attempts summary`, `blackdog attempts table`
- Codex links, evidence, and hooks: `blackdog codex link`, `blackdog codex coverage`, `blackdog codex history`, `blackdog codex hook stamp`
- repo lifecycle: `blackdog repo install`, `blackdog repo bind`, `blackdog repo table`, `blackdog repo archive`, `blackdog repo unarchive`, `blackdog repo unbind`, `blackdog repo analyze`, `blackdog repo scaffold`, `blackdog repo update`, `blackdog repo refresh`
- task execution and repair: `blackdog task begin`, `blackdog task show`, `blackdog task recover`, `blackdog task cancel`, `blackdog task reopen`, `blackdog task land`, `blackdog task reconcile-landing`, `blackdog task close`, `blackdog task cleanup`
- explicit low-level diagnosis and repair: `blackdog worktree preflight`, `blackdog worktree table`, `blackdog worktree preview`, `blackdog worktree start`, `blackdog worktree show`, `blackdog worktree land`, `blackdog worktree close`, `blackdog worktree cleanup`
- abandoned work is canceled by default; use `task reopen` only when it should return to normal execution

`worktree preflight` is optional read-only diagnosis; `task begin` is the one normal implementation entrypoint.

## Operator Guardrails

- Do not launch an external browser, use macOS `open`, use `xdg-open`, or run headed Playwright/browser sessions for agent verification unless the user explicitly asks for a user-visible browser; prefer Codex in-app browser tools or headless evidence.
- After `repo install`, `repo update`, or `repo refresh`, run `git status --short`; commit or land managed repo changes, or report the checkout as intentionally dirty before finishing.
- Before finishing implementation work, re-check branch and dirty state and do not leave uncommitted changes from your work.
- Treat the `target_branch` selected and recorded by Blackdog for the task as authoritative when landing and verifying the result; never assume it is `main` and never switch it manually.

## Fleet Scope

- Choose exactly one fleet scope: repeat `--project-root` for exact repos, repeat `--root` for read-only `blackdog.toml` discovery, or pass `--registry` for the explicit user-local registry.
- Discovery never populates the registry. Only bare `blackdog stats` has a compatibility registry fallback; `blackdog repo table` never selects registry scope implicitly.

## Docs To Review

- `AGENTS.md`
- `docs/INDEX.md`
- `docs/ARCHITECTURE.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`
