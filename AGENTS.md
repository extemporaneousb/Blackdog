# AGENTS

Blackdog is a machine-native task and attempt runtime for AI-driven local
development.

## Working Rules

- Blackdog is a public repository. Keep every tracked path, example, fixture,
  and generated artifact free of client or employer identifiers, personal
  workspace paths, non-public domains or email addresses, and other private
  context. Run `make public-check` before landing changes. Put additional
  machine-local forbidden terms in the gitignored `.public-denylist.local`
  file, one term per line.
- Keep the core dependency-light. Prefer the Python standard library unless a
  dependency is clearly justified.
- Use the current worktree's top-level `.VE` for Blackdog CLI invocations when
  it exists; prefer `./.VE/bin/blackdog` and not a different `blackdog` on
  `PATH`.
- Treat kept implementation edits in the primary worktree as a contract
  violation. `task begin` is the one normal implementation entrypoint: run it
  directly because it performs its own readiness checks and returns the
  branch-backed task worktree where kept edits belong. `worktree preflight` is
  optional read-only diagnosis, not a separate prerequisite for `task begin`.
- `.VE/` is not versioned. Each git worktree needs its own `.VE` rooted at
  that worktree; do not copy virtualenv directories between worktrees because
  they embed absolute paths.
- Blackdog uses WTAM for kept implementation changes. There is no non-WTAM
  implementation mode.
- The active shipped CLI surface is:
  - `blackdog init`
  - `blackdog repo analyze|bind|table|install|scaffold|update|refresh|archive|unarchive|unbind`
  - `blackdog local-repo add|list|remove`
  - `blackdog prompt preview|tune`
  - `blackdog attempts summary|table`
  - `blackdog codex link|coverage|history|hook stamp`
  - `blackdog stats`
  - `blackdog task begin|show|recover|land|reconcile-landing|close|cancel|reopen|cleanup`
  - `blackdog summary`
  - `blackdog snapshot`
  - `blackdog worktree preflight|table|preview|start|show|land|close|cleanup`
- Direct planned-task authoring is disabled by default. Keep normal repo work
  on `task begin`, `task land`, and the task recovery surfaces until a planned
  workflow has clear value again. For new work, do not pass `--workset` or
  `--task`; `task begin` creates the task envelope.
- Use `blackdog worktree preview` or `blackdog worktree start` only when
  resuming or repairing a known existing task id and you need to inspect the
  prompt receipt, repo contract inputs, branch/worktree plan, or worktree-local
  handler plan. Do not invent workset or task names.
- `blackdog.toml` owns explicit `[[handlers]]` blocks for repo-local env and
  runtime setup. Keep env/bootstrap policy there, not in the skill.
- `blackdog worktree start` is responsible for executing the handler plan:
  creating the worktree-local `.VE`, wiring the repo-root overlay, linking
  fallback root-bin tools, and writing the worktree-local `blackdog` launcher.
- Do not use or preserve deleted backlog/board/inbox/render flows, the removed
  supervisor flow, or the old bootstrap/tune implementations unless they are
  explicitly rebuilt on top of the typed core model.
- Repo lifecycle/operator surfaces such as `repo analyze|bind|table|install|update|refresh|archive|unarchive|unbind`,
  `local-repo add|list|remove`, `prompt preview|tune`,
  `attempts summary|table`, and `stats` are distinct from workset/task
  execution. Keep them in the product layer and do not encode them as
  workset/task semantics.
- Keep `[taxonomy].doc_routing_defaults` pointed at the docs agents must review
  before editing.
- Treat the file formats in `docs/FILE_FORMATS.md` as the contract for
  planning, runtime, and event artifacts.
- Keep repository policy in optional `[[guards]]` blocks in `blackdog.toml`.
  Blackdog owns the generic protocol and evidence; each target repo owns every
  policy decision made by its configured commands.
- Use `blackdog codex coverage|history|hook`, `repo table`, and `stats` as the
  reporting surfaces for implementation-like Codex work that did not enter
  Blackdog task execution.
- Keep skills thin. If a change adds logic that belongs in the CLI or library,
  move it there instead of expanding prompt-only behavior.
- Update docs in `docs/` when CLI behavior or file formats change.

## Target Package Boundaries

- Keep `blackdog_core` limited to durable planning/runtime contracts:
  profile/path resolution, canonical planning/runtime/event formats, typed
  claim/attempt semantics, and derived read models.
- `blackdog_core` explicitly excludes WTAM orchestration, bootstrap/refresh
  flows, skill generation, prompt tuning, and rendered UI surfaces.
- Keep `blackdog` limited to product-layer WTAM orchestration and repo
  lifecycle workflows on top of the typed core model.
- Keep `blackdog_cli` as a thin adapter over the shipped CLI surface. No
  domain logic belongs there.
- If a change needs client-specific context to make sense, it does not belong
  in core.

## Validation

- Run `make test` after meaningful Python changes.
- Run targeted CLI smoke checks when changing workset or WTAM behavior.

<!-- BLACKDOG MANAGED CONTRACT:BEGIN -->
## Blackdog Contract

This section is managed by `blackdog repo install` and `blackdog repo refresh`.
Keep repo-specific requirements outside this block.

- Use the repo-local `./.VE/bin/blackdog` when it exists instead of mutating Blackdog control files by hand.
- `blackdog.toml` is the machine-readable source of truth for handler setup and routed docs.
- `task begin` is the one normal implementation entrypoint. Run it directly; it performs its own readiness checks and returns the branch-backed task workspace where implementation edits belong.
- `./.VE/bin/blackdog worktree preflight --project-root .` is explicit read-only diagnosis. It does not start work and is not a separate prerequisite for `task begin`.
- Implementation edits belong only in the `workspace role: task` workspace returned by `task begin`; analysis-only work may stay in the current checkout but must not leave implementation edits there.
- When `task begin` runs from a normal linked worktree, Blackdog treats that linked branch as the target branch and lands the task back there.
- `.VE/` is unversioned and bound to one worktree path; create one per worktree and do not copy virtualenvs between worktrees.
- Before normal repo-skill implementation, create two mode-0600 UTF-8 temporary files outside the repo: `request_file` contains the exact triggering user request verbatim, and `execution_prompt_file` contains the composed goal, context, constraints, and done condition prompt. Set those shell variables to absolute paths and run the structured begin command below.
- Normal repo-skill implementation uses `./.VE/bin/blackdog task begin --project-root . --actor codex --execution-prompt-file "$execution_prompt_file" --prompt-mode skill --request-file "$request_file" --json`. `--actor` defaults to `codex`; the explicit value here makes ownership visible.
- Delete `request_file` and `execution_prompt_file` only when the structured `task begin` result contains both a nonempty `execution_prompt_replay_artifact_path` and a nonempty `user_prompt_replay_artifact_path`; otherwise preserve both temporary inputs.
- `blackdog codex link` is an opt-in continuation into a new Codex local chat for the active task worktree. It does not move the calling thread or create a Codex-managed worktree; Blackdog remains responsible for branch identity, landing, and cleanup.
- Before landing, set `completion_summary` to concise human-readable change statements: the first nonblank line becomes the Git subject and each later nonblank line is one major body item. Do not put Blackdog metadata in it. Build the `validation_args` shell array with at least one repeated `--validation` plus `NAME=passed|failed|skipped`; never submit placeholders or invented evidence.
- For new work, do not pass `--workset` or `--task`; `task begin` creates the task envelope and returns the task workspace.
- Abandoned work is canceled by default; use `task reopen` only when the work should re-enter the normal queue.
- For every structured result from `task begin`, `task show`, `task recover`, `task cancel`, `task reopen`, `task land`, `task reconcile-landing`, `task close`, and `task cleanup`, treat its `next_action` as the sole authority regardless of `operation_status`: execute its exact `argv` when `kind=command`; choose only a complete action from `choices` or `alternatives`; stop when `kind=blocked` or `kind=complete`; never infer an action from display text, reason or error prose, summaries, or compatibility recommendations.
- When repository policy enables automatic stale recovery, `task land` may internally run one exact task-worktree `git rebase --autostash`, execute the configured validation commands, and retry canonical landing. Trust the returned `next_action`: a commandless `automatic_stale_recovery_*` blocker is an exceptional handoff to the current landing agent. Preserve the retained task workspace, never choose ours/theirs, reset, force-update, or skip validation, and satisfy the typed `required_inputs` before retrying normal `task land`.
- Direct read-only `task show`, read-only `task recover`, and CLI `worktree show` may report bounded legacy landing detection. Execute only the exact read-only `next_action.argv`; never add `--apply` unless the explicit reconciliation proof returns its guarded apply action.
- If any task surface reports `next_action.action_id=retry_stale_claim_release_finalization`, execute that exact owner-task argv before claim-mutating begin/land/close or owner cancel/reopen. Its hidden request/decision guards are machine-emitted replay capabilities; never invent, edit, remove, or reuse them. Stop if Blackdog returns a blocked or conflict action.
- If any task surface reports `next_action.action_id=retry_task_close_finalization`, execute that exact argv until close completes. Its hidden close-request guard and terminal evidence are machine-emitted replay capabilities; never omit, edit, or reconstruct them. A blocked action has no recovery command and requires evidence inspection.
- Use low-level `worktree preview` or `worktree start` only when resuming or repairing a known existing task id; do not invent workset or task names.
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
