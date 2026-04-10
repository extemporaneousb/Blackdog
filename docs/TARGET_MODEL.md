# Target Runtime Model

This document defines the target runtime model Blackdog should converge on.

Use it to answer:

- what Blackdog is trying to become beyond the current shipped runtime
- which concepts should become first-class durable objects
- which current structures are worth keeping, reshaping, or replacing
- which decisions we should lock down before building more supervisor and planning features

Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for current package and boundary ownership.
Use [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md) for the current durable artifact contract.
Use [docs/architecture-diagrams.html](docs/architecture-diagrams.html) for a code-derived overview of the implementation as it exists today.

This document is intentionally directional. It does not silently change the current runtime contract. When a recommendation here becomes real behavior, update the architecture, CLI, and file-format docs to match.

## Product Direction

Blackdog should become an opinionated, reliable framework for multi-agent development in a git repository using a branch-backed worktree model.

The target product should:

- minimize agent bootstrap time while keeping startup behavior, repo policy, and doc review consistent
- connect tasks, prompts, runs, worktrees, branches, commits, and outcomes so the history of work is inspectable after the fact
- support both direct same-thread task execution and supervisor-driven child-agent execution
- keep prompt-to-result lineage with low enough overhead that teams actually leave it on
- make failures, pauses, land conflicts, wrong turns, and takeovers observable and recoverable instead of conversationally implicit
- handle both one-off tasks and long-running multi-phase plans without forcing the same amount of ceremony onto both

## Design Stance

Blackdog currently describes `blackdog_core` as a contract layer. That is accurate, but too abstract for daily use. In product and maintainer-facing language, the better term is runtime kernel: the boring, durable coordination layer every other surface depends on.

The runtime kernel should optimize for:

- explicit objects instead of raw nested dictionaries at the API boundary
- append-only history for events, attempts, results, and control messages
- small, typed write operations instead of ad hoc file mutation
- low-friction reads for agents that only need one task, one run, or one workset
- shared local runtime state across worktrees without polluting git history
- fast-forward-only landing and coherent task-linked commits

## Current Model Assessment

Blackdog already has solid foundations:

- `RepoProfile` and `BlackdogPaths` make the repository and shared control root real concepts
- `WorktreeSpec` and worktree inspection functions make WTAM safety facts explicit
- `BacklogTask`, `BacklogSnapshot`, and `RuntimeArtifacts` provide a usable task and runtime read model
- claims, approvals, inbox rows, events, and task results already exist as durable files
- the generated maintainer HTML gives a code-derived overview instead of relying only on prose

The current model is still mismatched to the product direction in a few important ways:

- branch identity is mostly incidental rather than first-class
- task execution history is smeared across claims, results, events, and supervisor artifacts rather than represented as one `TaskAttempt`
- supervisor runs exist, but there is no clean `wait` or `watch` primitive for long-lived coordination
- the inbox protocol is too freeform for reliable pause, takeover, replan, and handoff flows
- `epic`, `lane`, and `wave` act as durable planning truth even though they mostly serve as a view and scheduling hint
- unrelated backlog items still leak into normal agent interactions because the system lacks a strong workset or focus-scope concept
- validation policy is mostly static, which makes chained work either too noisy or too risky

## Target Properties

Any redesign should preserve these properties:

- deterministic durable state
- dependency-light core/runtime code
- repo-local control with shared mutable state outside git history
- worktree-native execution for kept changes
- low-context agent ergonomics
- inspectable lineage from request to landed commit
- resumable, takeover-safe execution after failures or interruptions

## First-Class Objects

The current runtime should grow toward this object model.

| Object | Purpose | Durable status |
| --- | --- | --- |
| `Repository` | Captures repo identity, target branch, control root, validation defaults, doc-routing defaults, and host policy knobs. | Already present in rough form via `RepoProfile` and `BlackdogPaths`; keep and formalize. |
| `Workspace` | Represents one concrete checkout with role, path, cleanliness, base commit, and linked branch state. | Partially present via worktree helpers; should become a formal read model. |
| `BranchBinding` | Associates a workspace or attempt with a named branch, base commit, landing target, and landed commit. | Missing as a first-class object. |
| `TaskSpec` | Stable description of a unit of work: title, intent, scope, dependencies, approvals, invariants, and validation obligations. | Present in rough form as backlog tasks; should stay durable. |
| `TaskState` | Derived summary of readiness, ownership, approval, active attempt, last outcome, and landing state. | Currently derived ad hoc; should become an explicit read model. |
| `TaskAttempt` | One concrete execution attempt of a task by one actor or run in one workspace. | Missing and should become first-class. |
| `Run` | A long-lived supervisor or direct execution session over a workset or plan slice. | Partially present in supervisor artifacts; needs a unified model. |
| `PromptReceipt` | Frozen record of what prompt/template/context/policy was actually sent for an attempt. | Missing and should become first-class. |
| `PlanGraph` | Explicit DAG of task dependencies, invariants, and concurrency hints. | Missing; current epic/lane/wave plan is an insufficient substitute. |
| `Workset` | Focus scope for a subset of the plan, such as an epic, a parking lot, or a direct ad hoc task group. | Missing and important for reducing agent noise. |
| `ValidationObligation` | Named validation requirement that can be satisfied, reused, or invalidated across task chains. | Missing. |
| `ControlMessage` | Typed command or request such as stop, pause, resume, takeover, or needs-input. | Inbox rows exist, but typing is too weak today. |
| `WaitCondition` | A durable description of what a run is waiting for and how it wakes up. | Missing. |
| `Result` | Durable outcome summary of an attempt or task, including checks, land outcome, and follow-up notes. | Present, but should gain stronger linkage to attempts and runs. |
| `Event` | Append-only fact stream linking tasks, attempts, runs, workspaces, prompts, and results. | Present, but correlation fields need to expand. |

The most important missing concepts are `TaskAttempt`, `Run`, `PromptReceipt`, `Workset`, `ValidationObligation`, and `WaitCondition`. Those are the concepts that would make Blackdog much better at observability, recovery, and multi-phase execution.

## Durable Facts vs Derived Views

One source of confusion today is that Blackdog mixes durable truth, mutable runtime state, append-only records, and rendered views in the same mental bucket. The target model should separate them cleanly.

### Checked-in durable specs

These are reviewable and belong in git when they exist:

- `blackdog.toml`
- optional checked-in plan specs for major work, imported into the runtime backlog
- checked-in prompt templates or skill policy that shape agent behavior

### Shared mutable runtime state

These remain outside git history in the shared control root:

- task claims
- approval rows
- active run state
- active attempt pointers
- wait conditions
- message acknowledgements and recovery cursors

### Append-only records

These should be durable, additive, and easy to audit:

- events
- typed control messages
- prompt receipts
- task-attempt records
- structured results
- run summaries

### Derived projections

These are generated views, not truth:

- `runtime_snapshot`
- HTML boards and reports
- next-runnable lists
- cleanup candidates
- human-oriented planning views such as lanes, waves, or swimlanes

## Recommended Storage Split

The current shared-local backlog is good for mutable coordination. It should remain out of git history.

The gap is that not every planning artifact belongs in the same place. Blackdog should move toward this split:

- keep runtime coordination state unversioned under the shared control root
- allow important plan specs to be checked in as reviewed artifacts
- treat the runtime backlog as the imported, actionable execution surface rather than the only place ideas can live

That means the answer to "should the backlog be checked in?" is "not as mutable runtime state." The better model is a checked-in plan spec plus an unversioned runtime execution state layered on top.

## Task Model

Tasks should remain the core executable unit, but the model around them needs to tighten up.

### `TaskSpec`

A task spec should capture:

- stable task identity
- title and intent
- narrow execution slice
- affected paths and relevant docs
- approvals and risk posture
- explicit dependencies
- invariants that must remain true
- validation obligations the task creates, satisfies, or invalidates
- workset membership

Blackdog already stores part of this, but dependencies, invariants, worksets, and validation obligations are underdeveloped.

### `TaskState`

Task state should be a derived object, not a bag of writeable status flags. It should answer:

- is the task ready to run
- is it blocked, and by what
- who owns it
- what is the latest active or terminal attempt
- has it landed
- is cleanup still required

### `TaskAttempt`

Every real execution should create a task attempt. That attempt should carry:

- `attempt_id`
- `task_id`
- actor
- run id if supervised
- workspace and branch binding
- prompt receipt
- started and ended timestamps
- execution status
- land outcome
- cleanup outcome

This is the missing bridge between a task spec and a coherent git/log history.

## Task Lifecycle

The most important lifecycle rule to lock down is that tasks and attempts are not the same thing.

Recommended lifecycle model:

- `TaskSpec` is durable planning truth
- `TaskState` is derived runtime state
- `TaskAttempt` carries execution status

Recommended derived task states:

- `draft`
- `ready`
- `awaiting-approval`
- `claimed`
- `running`
- `blocked`
- `landing`
- `done`
- `archived`

Recommended attempt states:

- `prepared`
- `running`
- `waiting`
- `blocked`
- `failed`
- `abandoned`
- `landed`
- `done`

We should not freeze every string immediately, but we should lock down the separation of concerns now:

- task specs do not own transient execution details
- attempts do own transient execution details
- task status is derived from task spec, approval state, dependency state, attempt state, and landing state

## Invocation, Handoff, and Takeover

Blackdog should treat invocation as "start an attempt against a task spec" rather than "flip a claim row and hope the rest is inferred."

Canonical operations should become:

- create task
- revise task
- reorder or rewire dependencies
- claim task
- start attempt
- attach prompt receipt
- record progress or checkpoint
- hand off attempt
- take over attempt
- record result
- land attempt
- complete task
- archive task
- clean up workspace

Takeover should preserve attempt history instead of creating conversational amnesia. A new actor can become responsible for an attempt, or a fresh attempt can branch from the previous one, but the lineage must stay explicit.

## Planning Model

The current `epic` / `lane` / `wave` model has value as a view, but it is too weak and too awkward to be the long-term durable planning truth.

Recommended direction:

- keep `epic` as an optional grouping label or workstream label
- replace implicit lane ordering with explicit DAG dependencies
- treat lanes and waves as generated planning views, not canonical identity or dependency storage
- introduce worksets for focused execution scopes such as `current-refactor`, `parking-lot`, or `release-blockers`
- allow optional concurrency groups or serialization hints when a strict DAG is not expressive enough

The current numbering friction is evidence that lanes and waves are being asked to do more than they are good at. A real graph plus generated views is the cleaner model.

## Validation Model

Blackdog needs to know more than "these are the checks attached to the task." It needs to know which validations matter, when they can be reused, and what invalidates them.

That suggests a new `ValidationObligation` model:

- a task can introduce or inherit validation obligations
- an attempt can satisfy an obligation
- later tasks can reuse that satisfaction until a changed path or failed step invalidates it
- the plan can express required validations at boundaries instead of forcing every task to repeat the same checks

This is the right way to make chained refactors faster without silently dropping safety.

## Git and WTAM Model

Repository, branch, and workspace semantics should be first-class in the runtime model.

Blackdog should aim for:

- one primary integration branch with fast-forward-only landing
- branch-backed task worktrees for kept changes
- explicit workspace bindings on attempts
- explicit landing metadata on attempts and results
- cleanup status as part of runtime state, not a side concern

We should also link attempts to landed commits more directly. The cleanest likely direction is commit trailers such as:

- `Blackdog-Task: BLACK-...`
- `Blackdog-Attempt: ATTEMPT-...`
- `Blackdog-Run: RUN-...`

That recommendation needs a real decision before implementation, but Blackdog should absolutely stop treating commit linkage as optional trivia.

## Supervisor and Waiting

The current supervisor can launch children and drain work, but it still behaves more like a one-shot command than a long-lived coordinating process.

The target model should support:

- long-lived runs over a workset or DAG slice
- typed wait conditions
- explicit wake-up reasons
- pause, stop, resume, takeover, and replan messages
- status views that answer "what is active, what is blocked, why, and what should happen next"

Useful wait conditions include:

- wait for dependency satisfaction
- wait for approval
- wait for child attempt update
- wait for process exit
- wait for clean primary worktree
- wait for landability
- wait for inbox/control message

This is the backbone for a supervisor that keeps monitoring instead of returning too early.

## Prompt and Result Lineage

Blackdog needs better prompt/result observability if it wants to improve agent reliability over time.

Every meaningful attempt should be able to answer:

- what user ask started this work
- what prompt template or skill policy shaped it
- which docs and context were injected
- what prompt packet the agent actually received
- what checkpoints, wrong turns, and recovery decisions happened during execution
- what result and landed commit came out of it

That is why `PromptReceipt`, `TaskAttempt`, and stronger `Result` linkage matter. Without them, prompt tuning and multi-agent policy become folklore instead of data.

## Higher-Level API Direction

Agents should not need to reason about raw state rows or hand-assembled event payloads unless they are doing low-level runtime maintenance.

Blackdog should expose higher-level operations over the durable artifacts:

- `plan draft`, `plan revise`, `plan import`, `plan graph`, `plan workset`
- `task create`, `task revise`, `task split`, `task merge`, `task archive`
- `attempt start`, `attempt checkpoint`, `attempt handoff`, `attempt takeover`, `attempt finish`
- `run start`, `run wait`, `run watch`, `run resume`, `run stop`
- `status task`, `status workset`, `status run`, `status doctor`

The exact command names can change. The important design rule is that the runtime should offer object-level operations so agents do not have to reconstruct state machines from scratch on every turn.

## Workset and Noise Reduction

Blackdog should make unrelated backlog work invisible by default.

The right default is:

- scope an agent or run to one workset unless broader visibility is required
- keep a parking lot or deferred backlog out of normal task selection
- let operators ask for the full backlog explicitly instead of forcing every agent to acknowledge ignored work

That makes the backlog a coordination surface rather than ambient noise.

## Recommended Lock-Down Decisions

These decisions are important enough to lock down before deeper implementation continues.

| Topic | Recommendation | Why | Status |
| --- | --- | --- | --- |
| Runtime terminology | Keep the package name `blackdog_core`, but describe it as the runtime kernel in docs and product language. | The current term is accurate but too abstract for daily reasoning. | Lock now |
| Task vs attempt | Freeze the rule that tasks are durable specs and attempts carry execution history. | Most other model confusion disappears once this is explicit. | Lock now |
| Mutable runtime storage | Keep mutable coordination state out of git history under the shared control root. | This avoids merge conflicts and keeps cross-worktree coordination practical. | Lock now |
| Checked-in planning | Introduce optional checked-in plan specs for reviewed intent, separate from mutable execution state. | Planning intent and runtime coordination have different storage needs. | Lock now |
| Inbox protocol | Move from freeform inbox rows to typed control messages with scope and acknowledgement. | Reliable pause, resume, takeover, and replan need structure. | Lock now |
| Branch/workspace identity | Make branch bindings and workspaces first-class runtime objects. | WTAM reliability depends on explicit bindings and cleanup state. | Lock now |
| Plan graph | Replace lanes and waves as durable truth with explicit dependencies plus generated views. | The current plan model is too indirect and too awkward to scale. | Prototype, then lock |
| Validation reuse | Add validation obligations and invalidation rules instead of repeating the same checks mechanically. | This improves both safety and throughput in chained refactors. | Prototype, then lock |
| Commit linkage | Decide whether task/attempt/run ids live in commit trailers, notes, or both. | Prompt/result/commit lineage will remain weak until this is explicit. | Clarify |
| Prompt receipt retention | Decide how much prompt material to store, redact, or summarize. | Observability matters, but prompt retention has cost and privacy tradeoffs. | Clarify |
| Wait execution model | Decide whether wait conditions are polled, event-driven, or hybrid. | This affects supervisor reliability and complexity. | Clarify |
| Parking-lot semantics | Decide whether parked work is one workset, a task status, or a separate backlog namespace. | We need a clean way to keep irrelevant work out of agent focus. | Clarify |

## Non-Goals

The target model should not:

- move product policy, HTML rendering, or editor integrations into the runtime kernel
- require every host repo to adopt checked-in plan specs on day one
- force simple one-off tasks through the same ceremony as multi-agent programs
- make agents parse raw artifact files when typed object-level operations can do the job
- confuse planning views with durable execution truth

## Suggested Migration Phases

### Phase 1: Freeze the target model in docs

- publish this design direction
- route the new doc into repo review defaults
- update status and architecture docs to distinguish current reality from target direction

### Phase 2: Add typed read and write models

- introduce first-class Python models for repository, workspace, task state, and control messages
- keep file formats stable while the in-process API becomes more explicit

### Phase 3: Introduce attempts and prompt receipts

- add `TaskAttempt` and `PromptReceipt`
- correlate results, commits, and events against attempts
- make failed or abandoned work recoverable instead of conversationally lost

### Phase 4: Improve planning semantics

- introduce a real dependency graph
- keep epics as optional grouping
- demote lanes and waves to rendered views
- add worksets and parking-lot support
- add validation obligations and invalidation rules

### Phase 5: Upgrade the supervisor model

- add `Run`, `WaitCondition`, typed takeover, and typed pause/resume flows
- add better status, doctor, and recovery surfaces
- make cleanup and resumed landing idempotent

### Phase 6: Tighten git lineage

- lock down commit linkage
- make landed-commit tracking part of the normal result model
- add cleanup and orphaned-workspace sweepers that can explain exactly what they are doing

## Questions To Keep Visible

The following questions should stay live until answered in code and docs:

- what is the smallest durable checked-in plan format that still supports review and replanning
- which parts of prompt receipts should be stored verbatim versus summarized
- how much of the supervisor should be event-driven versus explicit polling
- whether a direct same-thread run should also create a `Run` object, or only a `TaskAttempt`
- how a takeover should behave when the previous attempt has unlanded commits and a dirty workspace
- how to represent parked, deferred, or speculative work without making normal backlog reads noisy

## Decision Rule For New Work

Before adding more runtime behavior, ask:

1. Does this feature become simpler if `TaskAttempt`, `Workset`, or `ControlMessage` exists explicitly?
2. Is this durable truth, mutable runtime coordination, append-only history, or just a rendered view?
3. Does this belong in the runtime kernel, in Blackdog product code, or in an optional adapter?
4. Are we clarifying a decision this document already calls for, or are we broadening the model without first freezing the concept?

If the answer is unclear, resolve the documentation boundary first. Blackdog is at the stage where vocabulary and object shape are product work, not merely comments about product work.
