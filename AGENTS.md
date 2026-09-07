# AGENTS

Blackdog is a machine-native task and attempt runtime for AI-driven local
development.

## Working Rules

- Blackdog is public. Keep tracked files free of client identifiers, private
  domains, personal paths, credentials, and private prompt or model content.
  Put additional machine-local forbidden terms in the gitignored
  `.public-denylist.local`, one per line, and run `make public-check` before
  landing.
- Keep the core dependency-light. Prefer the Python standard library unless a
  dependency is clearly justified.
- Use `./scripts/blackdog` to execute this checkout's Blackdog source.
  Installed consumers use the standalone release; Blackdog does not need `.VE`.
- Never copy `.VE` between worktrees. A virtual environment contains absolute
  paths and belongs to one checkout.
- Kept implementation edits belong in the task worktree returned by
  `blackdog task begin`. Analysis-only work may remain in the current checkout.
- `task begin` is the normal implementation entrypoint. It performs readiness
  checks, creates a task for new work, and starts its branch-backed attempt.
- Treat every structured task result's `next_action` as the sole execution
  authority. Execute its exact `argv` for `kind=command`, choose only a
  complete emitted choice or alternative, and stop for `blocked` or `complete`.
  Never infer commands from display text, reasons, errors, or summaries.
- `blackdog.toml` owns repo-local environment handlers, repository guards,
  validation commands, landing policy, and routed documents. Do not move that
  policy into a generated skill.
- Repository lifecycle and reporting commands do not create or mutate tasks.
- Keep generated skills thin. Logic belongs in the CLI or library.
- Update `docs/` whenever CLI behavior or durable formats change.

## Shipped CLI

- `blackdog init`
- `blackdog repo analyze|bind|table|install|scaffold|update|refresh|migrate|archive|unarchive|unbind`
- `blackdog local-repo add|list|remove`
- `blackdog prompt preview`
- `blackdog attempts summary|table`
- `blackdog codex coverage|history|hook stamp`
- `blackdog stats`
- `blackdog task begin|show|recover|cancel|reopen|land|reconcile-landing|close|cleanup|outcome|validate`
- `blackdog summary`
- `blackdog snapshot`
- `blackdog worktree preflight|table`

Do not restore hidden task-authoring, automatic next-selection, low-level
worktree mutation aliases, removed prompt rewriting, or provider-specific chat
launchers. Higher-level task relationships are deferred until they can be
represented directly between executable tasks.

## Package Boundaries

- `blackdog_core` owns the durable task store, attempt and prompt provenance,
  task state transitions, event identity, atomic file mutation, and derived
  read models.
- `blackdog` owns Git worktree isolation, landing and cleanup transactions,
  handlers, repository lifecycle, guards, validation, provider references,
  observability, and prompt preview.
- `blackdog_cli` is a thin adapter over the shipped commands. It contains no
  domain logic.
- Client-specific semantics do not belong in core.

## Durable Contract

- `runtime.json` is the one canonical mutable task store.
- `events.jsonl` is append-only evidence for semantic mutations.
- Prompt replay artifacts are content-addressed under the private control root.
- A task has repository-unique identity and append-ordered attempts. One
  in-progress attempt is its exclusive execution claim.
- Task, attempt, event, and transaction identities must be deterministic where
  retry repair depends on them. Exact completed retries are no-ops.
- Unsupported older store versions fail closed. Historical control data is
  archived outside the active store and is never interpreted by the active
  runtime.
- The target branch recorded on the attempt is authoritative for landing and
  verification.
- Repository guards decide policy; Blackdog owns protocol and evidence.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks after lifecycle or command changes.
- Run `make public-check` before landing.

<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->
## Blackdog Contract

This section is managed by `blackdog repo install` and `blackdog repo refresh`.
Keep repo-specific requirements outside this block.

- Use the installed `blackdog` executable or the exact workspace executable returned by Blackdog; do not mutate control files by hand.
- `blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.
- `task begin` is the one normal implementation entrypoint. Run it directly; it performs its own readiness checks and returns the branch-backed task workspace where implementation edits belong.
- `blackdog worktree preflight --project-root .` is explicit read-only diagnosis. It does not start work and is not a separate prerequisite for `task begin`.
- Implementation edits belong only in the `workspace role: task` workspace returned by `task begin`; analysis-only work may stay in the current checkout but must not leave implementation edits there.
- When `task begin` runs from a normal linked worktree, Blackdog treats that linked branch as the target branch and lands the task back there.
- Blackdog does not require `.VE/`. Explicit project environment handlers may create one; virtual environments are unversioned and bound to one worktree path, so never copy them.
- Before normal repo-skill implementation, create two mode-0600 UTF-8 temporary files outside the repo: `request_file` contains the exact triggering user request verbatim, and `execution_prompt_file` contains the composed goal, context, constraints, and done condition prompt. Set those shell variables to absolute paths and run the structured begin command below.
- Normal repo-skill implementation uses `blackdog task begin --project-root . --actor codex --execution-prompt-file "$execution_prompt_file" --prompt-mode skill --request-file "$request_file" --json`. `--actor` defaults to `codex`; the explicit value here makes ownership visible.
- Delete `request_file` and `execution_prompt_file` only when the structured `task begin` result contains both a nonempty `execution_prompt_replay_artifact_path` and a nonempty `user_prompt_replay_artifact_path`; otherwise preserve both temporary inputs.
- Before landing, set `completion_summary` to concise human-readable change statements: the first nonblank line becomes the Git subject and each later nonblank line is one major body item. Do not put Blackdog metadata in it. Build the `validation_args` shell array with at least one repeated `--validation` plus `NAME=passed|failed|skipped`; never submit placeholders or invented evidence.
- For new work, do not pass `--task`; `task begin` creates the task and returns its workspace. A machine-emitted retry may identify an existing task explicitly.
- Abandoned work is canceled by default; use `task reopen` only when the work should re-enter the normal queue.
- For every structured result from `task begin`, `task show`, `task recover`, `task cancel`, `task reopen`, `task land`, `task reconcile-landing`, `task close`, and `task cleanup`, treat its `next_action` as the sole authority regardless of `operation_status`: execute its exact `argv` when `kind=command`; choose only a complete action from `choices` or `alternatives`; stop when `kind=blocked` or `kind=complete`; never infer an action from display text, reason or error prose, or summaries.
- When repository policy enables automatic stale recovery, `task land` may internally run one exact task-worktree `git rebase --autostash`, execute the configured validation commands, and retry canonical landing. Trust the returned `next_action`: a commandless `automatic_stale_recovery_*` blocker is an exceptional handoff to the current landing agent. Preserve the retained task workspace, never choose ours/theirs, reset, force-update, or skip validation, and satisfy the typed `required_inputs` before retrying normal `task land`.
- If any task surface reports `next_action.action_id=retry_task_close_finalization`, execute that exact argv until close completes. Its hidden close-request guard and terminal evidence are machine-emitted replay capabilities; never omit, edit, or reconstruct them. A blocked action has no recovery command and requires evidence inspection.
- Do not launch an external browser, use macOS `open`, use `xdg-open`, or run headed Playwright/browser sessions for agent verification unless the user explicitly asks for a user-visible browser. Prefer Codex in-app browser tools or headless evidence.
- After `repo install`, `repo update`, or `repo refresh`, run `git status --short`; commit or land managed repo changes, or report the checkout as intentionally dirty before finishing.
- Before finishing implementation work, re-check branch and dirty state and do not leave uncommitted changes from your work.
- Treat the `target_branch` selected and recorded by Blackdog for the task as authoritative when landing and verifying the result; never assume it is `main` and never switch it manually.

Document routing catalog: read only the entries relevant to the current task; do not load every document by default:
- `docs/INDEX.md`
- `docs/ARCHITECTURE.md`
- `docs/CLI.md`
- `docs/FILE_FORMATS.md`

Run the narrowest relevant validation after changes. Repo defaults:
- `PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'`

<!-- BLACKDOG MANAGED CONTRACT:END -->
