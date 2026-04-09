# Blackdog Charter

Blackdog exists to make repo-scoped backlog processing and multi-agent development practical, inspectable, and steerable.

## Product intent

Blackdog is not only a task list. The target product is a coordinating agent interface backed by repo-scoped local state, where:

- a user can express high-level project goals
- Blackdog can map those goals into epics, lanes, waves, and task-level execution slices
- Blackdog can launch or direct multiple child agents in parallel
- Blackdog can monitor progress, summarize what changed, surface drift, and redirect future work
- all of that coordination stays local to the repo in durable files instead of hidden global state
- mutable runtime state is shared across worktrees from one control root rather than checked into the artifact being built

## Core principles

- AI-native first: the system should be comfortable for an agent to read, update, and supervise without bespoke hidden state.
- Local-first and repo-scoped: the project owns its Blackdog contract, but mutable runtime state should live in one shared local control root rather than checked-in per-worktree files.
- Thin skills, real runtime: prompt scaffolds should point at a stable CLI and documented file formats.
- Worktree-native execution: multi-agent development should respect the operational lessons from worktree-based flows rather than treating them as an afterthought.
- Human-auditable control: users should be able to inspect backlog state, child-agent results, and steering decisions from repo files and generated views.

Layer ownership for the remodel is frozen separately in
[docs/BOUNDARIES.md](docs/BOUNDARIES.md). Use that document when deciding what
belongs in `core`, `blackdog`, or optional `extensions`.

## Near-term goals

- Get Blackdog itself using Blackdog as its primary working contract.
- Deploy Blackdog into one or more local host repositories and use it during real development.
- Clarify the product contract so the backlog can target the intended multi-agent supervisor system instead of only the current single-process runtime.

## Remodel checkpoints

The remodel should advance through explicit evaluation checkpoints so later
tasks can adjust sequencing and scope without drifting the target product.
Those checkpoints evaluate whether the next slice still converges on the
charter; they do not authorize ad hoc architecture changes by themselves.

Checkpoint sequence for the current remodel:

1. Core extraction checkpoint
   - Confirm the runtime contract that must remain repo-local and
     dependency-light.
   - Confirm that backlog/state/events/inbox/results, prompt shaping, and
     worktree lifecycle boundaries are still owned by real runtime code
     instead of prompt-only behavior.
2. Hardening checkpoint
   - Confirm that WTAM, shared control-root behavior, claims/results/inbox,
     and delegated child startup/landing semantics are reliable enough to
     support continued dogfooding.
   - Confirm that observed failures are turned into backlog evidence rather
     than silently papered over.
3. Blackdog product checkpoint
   - Confirm that Blackdog-on-Blackdog use still matches the manual-first
     repo contract and that docs, CLI behavior, and runtime artifacts tell
     one coherent story.
   - Confirm that the product is still described as a backlog runtime with
     supervision primitives until richer steering actually ships.
4. Adapter checkpoint
   - Confirm that host-repo bootstrap, repo-local skills, and related
     integration surfaces can adopt the remodel without special-case hidden
     state or per-repo architecture forks.

Each checkpoint should produce grounded evidence before the backlog is
reseeded:

- updated docs when the contract or target architecture changed
- task results that describe what changed, what was verified, and what
  remains open
- validation output appropriate to the touched surface
- explicit backlog follow-up tasks for unresolved gaps, rather than keeping
  the plan in chat-only memory

Resequencing is allowed between checkpoints. Charter drift is not. If a
checkpoint finds a better execution order, narrower intermediate slice, or
additional hardening task, add that work without changing the target
architecture. If a checkpoint finds that the target architecture itself is
wrong, update the charter and architecture docs first and only then reseed
follow-up work against the revised target.

## Blackdog repo working contract

The Blackdog repo itself should use Blackdog claims, results, inbox messages, and supervisor runs as the default coordination surface.

- Every meaningful change should either be claimed directly through the CLI or dispatched through `blackdog supervise run`.
- Every completed slice should leave a structured result in Blackdog's shared local control state.
- Blocked child runs are not noise; they are product evidence that should become backlog follow-up work.
- The generated HTML view and supervisor run directories are part of the repo's normal operating surface, not side artifacts to ignore.

## Current implementation assessment

Well encoded today:

- repo-local backlog artifacts and file contracts
- task claims, approvals, inbox messages, structured results, and HTML status output
- backlog plan structure with epics, lanes, and waves
- project-local skill generation for host repositories

Partially encoded today:

- multi-agent coordination primitives exist, but only as building blocks
- the worktree model is now explicit, mutable runtime state is shared from one control root, and delegated child runs land through the same WTAM lifecycle as direct work
- a supervisor run can drain work, reread backlog/state while active, refresh repo-local status views, and honor simple inbox stop control messages
- child-agent launch, monitoring, and worktree lifecycle exist, but still require better active-run steering and cleanup ergonomics
- backlog planning exists in the file format, but management UX is still task-by-task
- host-repo installation works, but it is not yet a one-command experience

Not yet encoded in runtime behavior:

- interactive drift assessment and redirection workflows
- a rollout playbook based on real host-repo adoption
- richer supervisor steering than boundary stop controls plus active-run backlog rereads

## Success criteria

Blackdog should eventually support a development run model where a user can:

1. define or revise a project goal at a high level
2. have Blackdog convert that into a structured backlog with parallel work lanes
3. start a multi-agent run against that backlog
4. ask for current progress and receive a grounded summary from repo-local state
5. redirect the run through the coordinating agent without losing execution history

## Scope boundary for the current release line

Until supervisor and worktree support land, Blackdog should describe itself as a backlog runtime with multi-agent supervision primitives, not as a complete multi-agent orchestration system.
