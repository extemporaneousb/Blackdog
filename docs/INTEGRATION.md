# Host Repo Integration

This document describes the current integration path for adopting Blackdog in another local repository.

## What exists today

- a one-command repo bootstrap once Blackdog is installed in the local Python environment
- repo-local profile in `blackdog.toml`
- mutable backlog/runtime artifacts under a shared git control root
- project-local skill scaffold under `.codex/skills/blackdog/`
- a branch-backed `blackdog worktree` lifecycle for implementation tasks
- CLI support for task intake, claims, approvals, comments, inbox messaging, task results, and HTML rendering
- an initial supervisor runner that can launch child commands against runnable tasks, with a default preference for the desktop Codex exec runtime
- a supervisor run that drains active work, refreshes repo-local status views, and honors inbox `stop` messages
- a static backlog index that embeds the current snapshot JSON and links directly to artifact files on disk

## What does not exist yet

- a single command that both installs Blackdog into a host environment and bootstraps the repo from scratch
- richer active-run steering beyond simple stop control messages
- a write-enabled runtime UI for approvals or steering from the browser

## Current setup flow

1. Install Blackdog into a local Python environment available to the host repo.
   Today that can be an editable checkout or a Git install such as `python -m pip install -e /path/to/blackdog` or `python -m pip install git+<github-url>`.
2. Run `blackdog bootstrap --project-root /path/to/repo --project-name "Repo Name"`.
   If needed, `blackdog-skill new backlog` remains as a compatibility wrapper around the same bootstrap flow.
3. Review `blackdog.toml` and tune taxonomy, validation commands, and doc routing for the host repo.
   Review `paths.control_dir` and `paths.worktrees_dir` in particular; the defaults are `@git-common/blackdog` and `../.worktrees`, so runtime state is shared across worktrees and implementation work lands through sibling task worktrees rather than nested repo-runtime directories.
   Set `taxonomy.doc_routing_defaults` to the minimum repo docs agents must review before making kept changes; Blackdog emits that list into the generated project-local skill.
4. Commit `blackdog.toml` and the project-local skill scaffold if they are part of the repo's working contract.
   Do not plan around checking in mutable runtime files; Blackdog now defaults to a shared local control root outside the built artifact.
5. If you later change `blackdog.toml`, regenerate the tailored skill with `blackdog-skill refresh backlog --project-root /path/to/repo`.
6. In each fresh git worktree, create that worktree's own `.VE/` (or equivalent repo-local environment) before running repo-local commands. Do not copy `.VE/` directories between worktrees; virtualenvs embed absolute paths.
7. Treat implementation edits in the primary worktree as a contract violation. Start with `blackdog worktree preflight`; if it reports `primary worktree: yes`, do not edit in that checkout and create or enter a task worktree with `blackdog worktree start --id TASK` first. Analysis-only work can stay in the current checkout.
8. Use `blackdog validate`, `blackdog summary`, `blackdog next`, `blackdog worktree preflight|start|land|cleanup`, `blackdog claim`, `blackdog result record`, and `blackdog render` during normal work. Open `backlog-index.html` directly and reload it when you want the latest state.

## How agents discover the Blackdog contract

Bootstrap creates a project-local skill at `.codex/skills/blackdog/` with:

- `SKILL.md`: the repo-specific operating instructions
- `agents/openai.yaml`: UI-facing metadata for skill lists and default prompts

That generated skill is tailored from the current Blackdog profile. It includes the repo name, runtime paths, validation commands, `taxonomy.doc_routing_defaults`, and the expected operator model for direct work and supervisor-driven work.

Blackdog does not currently shell out to an external skill-authoring workflow at bootstrap time. Instead, it generates and refreshes this project-local skill deterministically from `blackdog.toml` so the skill stays aligned with the repo contract.

## Recommended repo-specific configuration review

- `[taxonomy].buckets`: align with the host repo's work categories
- `[taxonomy].domains`: reflect the host repo's meaningful system boundaries
- `[taxonomy].validation_commands`: set the narrowest standard checks an agent should run by default
- `[taxonomy].doc_routing_defaults`: point at the docs an agent must review before changing code; Blackdog emits this list into the generated skill, so keep it limited to the required review set
- `[rules].default_claim_lease_hours`: match expected task duration
- `[rules].require_claim_for_completion`: keep this enabled unless the repo intentionally allows ad hoc completions
- `[paths].control_dir`: keep the git-common default unless the host repo has a strong reason to relocate mutable runtime state
- `[paths].worktrees_dir`: prefer a sibling worktree base or an explicit `.worktrees` symlink target over an in-repo runtime directory
## Adoption checklist for a first pilot

- Confirm the repo can tolerate a shared local Blackdog control root that is not part of the built artifact.
- Confirm the repo has a stable Python entrypoint for Blackdog.
- Confirm each task worktree can bootstrap its own repo-local `.VE/` without copying virtualenv directories from another checkout.
- Create one real epic with at least two parallel lanes.
- Require claims and structured task results for the pilot slice.
- Capture friction points as follow-up tasks in the host repo or in Blackdog's own backlog.

## Expected operator model today

Today, Blackdog works best as a coordinating contract used by a foreground agent or a supervisor run. The agent reads the repo-local backlog, claims work, records results, and uses inbox messages for coordination, while the static HTML index surfaces task state and artifact links without becoming a second source of truth.

For implementation tasks, the intended operator model is now explicit and hard-gated: start with `blackdog worktree preflight`, and if it reports `primary worktree: yes`, do not edit there. Create a branch-backed task worktree from the primary checkout, make changes there, and land with fast-forward semantics. Analysis-only work can stay in the current checkout.

The generated skill should mirror that hard gate and enumerate the repo docs from `taxonomy.doc_routing_defaults` so agents see both the worktree contract and the required review set before they edit.

Delegated child runs use the same lifecycle: the coordinating supervisor stays in the primary worktree, launches each child in a branch-backed task worktree, expects a commit on that branch, and lands it through the primary worktree after a successful run. WTAM is the only kept-change implementation path.

Each worktree should also carry its own repo-local `.VE/` when the host repo uses one. Blackdog will prefer `./.VE/bin/blackdog`, but operators must create that environment per worktree rather than copying it across checkouts.

Version 0 supervisor steering is intentionally narrow. `stop` is a boundary control checked while the run is active; it prevents new launches while already-running child claims continue until the child exits or times out. The run rereads backlog and state while it is active, so newly added or newly unblocked tasks can become eligible before the run drains to idle. Tasks completed during that run stay visible in the execution map until the next run starts and performs its cleanup sweep.

As the supervisor grows beyond the current runner, this guide should expand to cover agent pools, richer steering, launch configuration, and run monitoring.
