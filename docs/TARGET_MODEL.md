# Target Runtime Model

This document defines the target Blackdog product/runtime model.

Use it to answer:

- what Blackdog is trying to become beyond the current shipped runtime
- which concepts should become first-class durable objects
- which current structures are worth keeping, reshaping, or removing
- which decisions should be locked down before broader supervisor and planning work continues

Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for current package and boundary ownership.
Use [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md) for the current durable artifact contract.
Use [docs/architecture-diagrams.html](docs/architecture-diagrams.html) for a code-derived overview of the implementation as it exists today.

This document is directional. It does not silently change the current runtime contract. When a recommendation here becomes real behavior, update the architecture, CLI, and file-format docs to match.

## Scope And Boundaries

This document describes the target model for Blackdog as a whole, not just `blackdog_core`. That only works if the package boundaries stay explicit.

- `blackdog_core` is the runtime kernel. It owns durable artifacts, typed read/write models, repository and workspace identity, task and attempt identity, derived runtime state, append-only records, and the invariants other layers depend on.
- `blackdog` is the product/orchestration layer. It owns WTAM lifecycle orchestration, workset execution, task shaping, prompt shaping, takeover and recovery behavior, direct execution, and supervisor behavior.
- viewers and adapters are projections and entrypoints. They render or expose runtime state, but they are not durable truth and they should not become a second control plane.

The rest of this document describes the target product/runtime model while treating the runtime-kernel boundary as a hard design constraint.

## Product Direction

Blackdog should become an opinionated, reliable framework for multi-agent development in a git repository using a branch-backed worktree model.

The target product should:

- minimize startup and bootstrap time while imposing consistent behavior around onboarding, repo policy, branching and landing, testing, and reporting
- connect tasks, prompts, attempts, workspaces, branches, commits, and outcomes so work is inspectable after the fact
- support both same-thread execution and supervisor-driven child-agent execution
- store full prompt receipts and execution history with low enough friction that teams actually leave the feature on
- make failures, pauses, land conflicts, wrong turns, and takeovers observable and recoverable instead of conversationally implicit
- support both one-off tasks and longer parallel work without treating the backlog as a dumb queue of isolated jobs

## Design Stance

The runtime kernel should optimize for:

- explicit typed objects at the API boundary
- append-only history for events, attempts, results, and control traffic
- small typed operations instead of ad hoc file mutation
- low-friction reads for agents that only need one task, one attempt, or one workset
- first-class repository, workspace, branch, and integration-branch identity
- clean git history with task-linked commits and predictable land behavior
- clear observability of what Blackdog itself is doing and how well it is working

## Target Properties

Any redesign should preserve these properties:

- deterministic durable state
- dependency-light kernel code
- shared runtime state outside git history
- worktree-native execution for changes we intend to land
- low-context agent ergonomics
- inspectable lineage from request to landed commit
- resumable, takeover-safe execution after failures or interruptions
- strong observability and measurable runtime performance

## Terms of Art

This section introduces a shared lexicon for the rest of the document.

| Term | Meaning in this document | Why it matters for Blackdog |
| --- | --- | --- |
| runtime kernel | The boring durable coordination layer that owns canonical state and execution semantics. | This is the right maintainer-facing description of `blackdog_core`. |
| integration branch | The branch a workset lands into. This may be `main` or a first-class feature branch beneath `main`. | Blackdog must support experimentation and integration below `main`, not just direct landing into `main`. |
| kept changes | Changes we intend to preserve and land, rather than temporary exploration or discarded local output. | WTAM rules exist to protect kept implementation work. |
| repo map | A compact structural summary of the codebase, usually focused on files, symbols, and relationships. | Blackdog needs a better low-token way to orient agents before task execution. |
| workset | The primary planning and execution container. A workset owns a task DAG, scope, visibility boundary, policies, and target integration branch. | This is the recommended replacement for `epic` / `lane` / `wave` as durable planning truth. |
| task spec | The durable specification of one executable unit of work inside a workset. | Tasks stay the core executable unit. |
| task state | The derived runtime view of a task: readiness, ownership, blocking reason, latest attempt, land state, and cleanup state. | Agents should read this, not reconstruct it from raw files. |
| task attempt | One concrete execution of one task by one actor in one workspace. | This is the missing execution-history object. |
| workset execution | The coordination context above individual task attempts. It may represent same-thread execution or supervisor-led execution over part of a workset. | This replaces the overloaded idea of a generic `Run`. |
| prompt receipt | The full prompt packet actually sent to the model, plus the repo state, docs, and policies that shaped it. | This is the missing prompt/result lineage artifact. |
| control message | A typed steering message such as pause, stop, resume, takeover, replan, or request-input. | Control traffic should be explicit rather than freeform inbox text. |
| wait condition | A durable description of what a workset execution is waiting for and how it wakes up. | Waiting must be a runtime concept, not just a poll loop or chat convention. |
| lineage | The chain linking request, workset, task, attempt, prompt receipt, workspace, branch, result, and landed commit. | This is the audit trail Blackdog is trying to make real. |

## Current Model Assessment

Blackdog already has solid foundations:

- `RepoProfile` and `BlackdogPaths` make the repository and shared control root real concepts
- `WorktreeSpec` and worktree inspection functions make WTAM safety facts explicit
- `BacklogTask`, `BacklogSnapshot`, and `RuntimeArtifacts` provide a usable task and runtime read model
- claims, approvals, inbox rows, events, and task results already exist as durable files
- the viewer layer can already generate code-derived overviews, which is useful, but those views are not part of the runtime kernel

The current model is still mismatched to the product direction in a few important ways:

- branch and integration-branch identity are still too incidental
- task execution history is spread across claims, results, events, and supervisor artifacts instead of being one first-class `TaskAttempt`
- there is no crisp execution object above attempts
- the inbox protocol is too freeform for pause, takeover, replan, and recovery flows
- the backlog still surfaces unrelated work too often
- `epic`, `lane`, and `wave` mix grouping, ordering, and execution scope in awkward ways
- validation is mostly attached as static per-task checks, which makes chained work either noisy because checks repeat or risky because agents start skipping them informally

## What We Should Learn From Adjacent Systems

The detailed comparative research does not belong in this document. What does belong here is the short list of lessons Blackdog should carry forward from adjacent tools and runtimes.

- Structural repo orientation matters. A repo map or internal API map is more useful than repeatedly restating the repo in prose.
- Execution history needs a canonical artifact. Blackdog should treat `TaskAttempt` the way other systems treat a trajectory or durable thread history.
- Multi-agent work needs explicit routing and ownership. Shared chat alone is not a reliable control plane.
- Waiting is a first-class runtime behavior. Long-lived coordination should survive terminal exits and process restarts.
- Git hygiene and recovery are product features. Clean landing, clear lineage, and predictable cleanup are not secondary concerns.

## Target Object Model

Blackdog should converge on a small number of primary objects.

| Object | Purpose | Durable status |
| --- | --- | --- |
| `Repository` | Captures repo identity, control-root location, doc-routing defaults, validation defaults, and allowed integration branches. | Already present in rough form; keep and formalize. |
| `Workspace` | Represents one concrete checkout with path, cleanliness, role, base commit, current branch, and target integration branch. | Partially present; should become a formal read model. |
| `Workset` | Primary planning and execution container. Owns scope, task DAG, visibility boundary, policies, and target integration branch. | Missing as a first-class object. |
| `TaskSpec` | Stable description of one executable task inside a workset. | Present in rough form; keep and tighten. |
| `TaskState` | Derived runtime view of readiness, ownership, blocking, latest attempt, landing, and cleanup. | Currently derived ad hoc; should become explicit. |
| `TaskAttempt` | One concrete execution of one task by one actor in one workspace. | Missing and should become first-class. |
| `WorksetExecution` | Coordination context over part or all of a workset, whether same-thread or supervisor-led. | Partially implied today; should become explicit. |
| `PromptReceipt` | Frozen record of the exact prompt packet, shaping inputs, repo state, and policy context used for an attempt. | Missing and should become first-class. |
| `WaitCondition` | Durable description of what a workset execution is waiting on. | Missing. |
| `ControlMessage` | Typed steering message used for pause, resume, takeover, replan, stop, and request-input flows. | Inbox rows exist, but typing is too weak. |
| `Result` | Durable outcome summary linked to an attempt, workspace, branch, checks, and land state. | Present, but linkage needs to strengthen. |
| `Event` | Append-only fact stream linking worksets, tasks, attempts, prompt receipts, results, and commits. | Present, but correlation needs to expand. |

This list is intentionally smaller than the previous draft. In particular, the planning graph lives inside `Workset` rather than becoming a separate top-level object, and validation stays attached to worksets and tasks rather than being promoted to a separate durable object yet.

## Planning And Backlog Model

The backlog should not be modeled as a queue of interchangeable jobs for dumb workers. It should be modeled as a coordination surface for shaped work.

The recommended durable planning model is:

- a backlog contains one or more worksets
- a workset owns the task DAG, scope, visibility boundary, policies, and target integration branch
- a task spec is the executable unit inside that workset

This is the recommended simplification:

- retire `epic`, `lane`, and `wave` as durable planning truth
- keep only `workset` plus the task DAG
- generate any human-oriented queue or swimlane views from that model instead of storing them as canonical structure

That approach solves two current problems at once:

- it gives Blackdog one primary execution scope, which makes unrelated work easier to hide
- it separates planning truth from rendered planning views

The default interaction model should be workset-scoped. An agent working one workset should not need to see the full backlog unless the operator asks for it explicitly.

## Task Model

Tasks remain the core executable unit, but their contract needs to tighten.

### `TaskSpec`

A task spec should capture:

- durable task identity
- title and intent
- the specific outcome or boundary the task is responsible for
- affected paths and relevant docs
- dependencies within the workset DAG
- invariants that must remain true
- validation requirements or policy hooks
- task kind

The task spec should be narrow and intuitive for agents. It should be pre-cleared enough that an agent can start with minimal context and still act correctly.

### `TaskState`

Task state should be a derived object, not a bag of writeable flags. It should answer:

- is the task ready to run
- is it blocked, and by what
- who owns it
- what is the latest active or terminal attempt
- has it landed into its target integration branch
- is cleanup still required

Agents should normally read a pre-derived task state rather than recompute these answers from raw artifacts on every turn.

### `TaskAttempt`

Every real execution should create a task attempt. A task attempt should carry:

- `attempt_id`
- `task_id`
- actor identity
- workset execution id when applicable
- workspace and branch identity
- prompt receipt
- started and ended timestamps
- execution status
- result linkage
- land outcome
- cleanup outcome

This is the missing bridge between a task spec and a coherent git and runtime history.

## Task Kinds And Execution Boundary

Blackdog should start with one clear task kind and leave room for more later.

The initial task kind should be an implementation task:

- the invoking side defines the workset, task spec, target integration branch, attempt creation, prompt receipt, and execution policy
- the task agent owns the task-local work inside its assigned workspace, including edits, required checks, local commits or checkpoints, and result recording
- the invoking side or workset execution owns integration decisions, landing, cleanup, completion, and takeover unless that authority is explicitly delegated

That boundary makes the current WTAM model easier to reason about and gives Blackdog room to experiment with other task kinds later without pretending every task behaves the same way.

## Workset Execution, Waiting, And Control

`WorksetExecution` is the execution object above individual task attempts. The name can change later if a better one emerges, but the role should be kept.

A workset execution should own:

- the selected workset or DAG slice
- the target integration branch
- active and recent task attempts
- workset-level status and metrics
- wait conditions and wake-up reasons
- control messages and takeover state

This object should cover both same-thread execution and supervisor-led execution. The difference between those modes is not whether the object exists. The difference is how many attempts it coordinates and whether there are child agents involved.

Control messages should be typed and narrow. They exist to steer work, not to force constant chatter from task agents.

Useful control messages include:

- pause
- stop
- resume
- takeover
- replan
- request-input

Useful wait conditions include:

- wait for dependency satisfaction
- wait for approval
- wait for task-attempt update or result
- wait for process exit
- wait for a clean landing workspace
- wait for landability into the target integration branch

The important rule is that Blackdog should be able to monitor work without forcing task agents into artificial message-passing behavior.

## Git And Storage Model

Repository, branch, and workspace semantics should be first-class in the runtime model.

Blackdog should support:

- `main` as the ultimate integration branch
- first-class feature integration branches beneath `main`
- branch-backed worktrees for kept implementation changes
- explicit workspace and branch identity on attempts and results
- fast-forward-oriented landing into the workset target branch

Mutable runtime coordination state should remain outside git history under the shared control root. That includes claims, active executions, wait conditions, acknowledgements, and similar coordination state.

For now, checked-in plan artifacts are not part of the target model. The repo should continue to treat Blackdog runtime state as shared local coordination state rather than checked-in planning truth.

Blackdog should still aim for much stronger git linkage than it has today. The exact mechanism is still open, but tasks, attempts, worksets, and landed commits should stop being only loosely inferred from each other.

## Prompt And Result Lineage

Prompt and result lineage should become much stronger.

Every meaningful attempt should be able to answer:

- what request or operator action started the work
- which workset and task it belongs to
- which model and reasoning mode were used
- which docs, templates, and policies were injected
- which repository and branch state the agent saw
- what full prompt packet the agent actually received
- what happened during execution
- what result, branch state, and landed commit came out of it

`PromptReceipt` should store the full prompt packet, not an induced summary. It should also include the commit hashes or other repo-state identifiers needed to confirm what code the agent was actually looking at.

## Observability And Validation

Blackdog needs stronger observability of its own behavior.

At a minimum, the runtime should make it easy to answer:

- what worksets are active
- what tasks and attempts are running
- what is blocked
- why it is blocked
- what changed
- what should happen next

Validation also needs a better model than a flat list of per-task checks. For chained work, Blackdog should be able to express which validations were satisfied, which are still reusable, and which later changes invalidated them. The exact mechanism is still open, but the product requirement is clear: make chained work faster without weakening safety.

## Recommended Lock-Down Decisions

These decisions are important enough to lock down now:

- describe `blackdog_core` as the runtime kernel in docs and product language
- make first-class feature integration branches part of the repository model
- separate `TaskSpec`, `TaskAttempt`, and `WorksetExecution`
- replace `epic`, `lane`, and `wave` with `Workset` plus the task DAG
- keep mutable coordination state out of git history
- store full prompt receipts
- move from freeform inbox rows toward typed control messages and wait conditions
- keep checked-in planning artifacts out of scope for now

## Open Questions

These questions should stay visible while implementation catches up:

- what is the minimum useful shape of a repo map for agent bootstrap
- what is the exact commit-linkage mechanism between attempts, worksets, and landed commits
- which parts of waiting and control should be event-driven versus polled
- how many task kinds Blackdog should add beyond the initial implementation task
- whether `WorksetExecution` is the right long-term name for the coordination object above attempts

## Suggested Migration Direction

The likely migration path is:

1. tighten the document and runtime vocabulary
2. add explicit typed models for repository, workspace, workset, task state, and control traffic
3. add `TaskAttempt` and `PromptReceipt`
4. shift planning to `Workset` plus the task DAG
5. add `WorksetExecution`, typed waits, typed control messages, and stronger observability
6. strengthen git linkage, cleanup, and validation reuse

## Decision Rule For New Work

Before adding more runtime behavior, ask:

1. Is this durable truth, mutable coordination state, append-only history, or a rendered view?
2. Does this belong in the runtime kernel, in Blackdog product code, or in a viewer or adapter?
3. Does the feature become simpler if `Workset`, `TaskAttempt`, or `WorksetExecution` is modeled explicitly first?
4. Are we clarifying the model, or are we adding more terminology than the problem actually needs?

If the answer is unclear, resolve the vocabulary and boundary first. Blackdog is at the stage where object shape and control-plane clarity are product work.
