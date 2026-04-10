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

This document is intentionally directional. It does not silently
change the current runtime contract. When a recommendation here
becomes real behavior, update the architecture, CLI, and file-format
docs to match.

## Product Direction

Blackdog should become an opinionated, reliable framework for
multi-agent development in a git repository using a branch-backed
worktree model.

The target product should:

- minimize agent startup and bootstrap time while imposing consistent
  agent behavior across a number of concerns, e.g., agent startup,
  repo policies and agent onboarding, commit/branch/merge/etc. repo
  operations, testing, and completing/landing work and reporting. <xxx>Are there more to
  enumerate?</xxx>
- connect tasks, prompts, runs, worktrees, branches, commits, and
  outcomes so the history of work is inspectable after the fact
- support both direct same-thread task execution and supervisor-driven
  child-agent execution
- keep prompt-to-result lineage with low enough overhead that teams
  actually leave it on
- make failures, pauses, land conflicts, wrong turns, and takeovers
  observable and recoverable instead of conversationally implicit
- handle both one-off tasks and long-running multi-phase plans without
  forcing the same amount of ceremony onto both

## Design Stance

Blackdog is composed of: 

 `blackdog_core` - a module which provides  runtime kernel
facilitating a durable coordination layer that is the boring, durable
coordination layer every other surface depends on.


The runtime kernel should optimize for:

- [Shouldn't we use pydantic here?] explicit objects instead of raw nested dictionaries at the API boundary
- append-only history for events, attempts, results, and control messages
- [Which files are transient, which are not, should we consider a
  small embedded DB?] small, typed write operations instead of ad hoc file mutation
- low-friction reads for agents that only need one task, one run, or one workset
- [This has to be about branch too, in practice, having multiple
  feature branches going where multiple agents make worktrees from a feature
  branch managed by a supervisor agent who takes that work and lands
  it in main.] shared local runtime state across worktrees without polluting git history
- [I'm not sure this is the right way to say this, what I want to see
  is a really clean git history when i do git log that shows a
  standard-formatted commit message connected to one or more tasks] fast-forward-only landing and coherent task-linked commits 

## Current Model Assessment

Blackdog already has solid foundations:

- `RepoProfile` and `BlackdogPaths` make the repository and shared control root real concepts
- `WorktreeSpec` and worktree inspection functions make WTAM safety facts explicit
- `BacklogTask`, `BacklogSnapshot`, and `RuntimeArtifacts` provide a usable task and runtime read model
- claims, approvals, inbox rows, events, and task results already exist as durable files
- [What is this refer to are we still in the core here?] the generated maintainer HTML gives a code-derived overview instead of relying only on prose

The current model is still mismatched to the product direction in a few important ways:

- branch identity is mostly incidental rather than first-class
- task execution history is smeared across claims, results, events, and supervisor artifacts rather than represented as one `TaskAttempt`
- supervisor runs exist, but there is no clean `wait` or `watch` primitive for long-lived coordination
- the inbox protocol is too freeform for reliable pause, takeover, replan, and handoff flows
- `epic`, `lane`, and `wave` act as durable planning truth even though they mostly serve as a view and scheduling hint
- unrelated backlog items still leak into normal agent interactions because the system lacks a strong workset or focus-scope concept
- [not sure i understand why validation policy being static makes this
  so, but i do agree with the observation that this is what impedes
  chained work] validation policy is mostly static, which makes chained work either too noisy or too risky

## Target Properties

Any redesign should preserve these properties:

- deterministic durable state
- dependency-light core/runtime code
- repo-local control with shared mutable state outside git history
- worktree-native execution for [what does this word kept mean here] kept changes
- low-context agent ergonomics [can we prime agents so as to decrease
  startup time so that agents get up to speed and then receive the
  actual work?] 
- inspectable lineage from request to landed commit
- resumable, takeover-safe execution after failures or interruptions
- observable, clear ability to understand what blackdog itself is
  doing and measure performance

## Terms of Art

This section deliberately introduces a shared lexicon. Most of these terms are used across agent systems, workflow engines, or MCP servers, but Blackdog should define them explicitly rather than assuming everyone means the same thing.

| Term                   | Meaning in this document                                                                                                                                     | Why it matters for Blackdog                                                                  |
|------------------------|--------------------------------------------------------------------------------------------------------------------------------------------------------------|----------------------------------------------------------------------------------------------|
| runtime kernel         | The boring, durable coordination layer that owns canonical state and execution semantics.                                                                    | This is the better maintainer-facing name for what `blackdog_core` is trying to be.          |
| control plane          | The layer that decides what should run, who owns it, what it is waiting on, and how it is steered.                                                           | Blackdog needs a clearer control plane for planning, claims, takeovers, and waits.           |
| data plane             | The layer that actually executes work: shell commands, edits, tests, tool calls, and land operations.                                                        | WTAM, tool calls, and child-agent execution belong here.                                     |
| repo map               | A compact structural summary of the codebase, usually focused on files, symbols, and relationships.  [Should this be an index or something more complicated] | Useful for bootstrap minimization and low-token code orientation.                            |
| trajectory             | A step-by-step record of an agent run, usually including prompts, actions, and observations.                                                                 | Blackdog should treat `TaskAttempt` as the durable, task-shaped analogue of a trajectory.    |
| prompt receipt         | The normalized prompt packet actually sent to the model, including injected docs, templates, and shaping.                                                    | This is the missing artifact for prompt/result lineage.                                      |
| handoff                | A transfer of responsibility from one agent or process to another.                                                                                           | Blackdog needs explicit handoff and takeover semantics rather than conversational inference. |
| agent runtime          | The execution environment that manages agent identity, lifecycle, communication, and monitoring.                                                             | Blackdog increasingly needs one for supervisors and background runs.                         |
| topic/subscription     | Pub-sub addressing where messages are published to topics and delivered according to subscriptions.                                                          | Useful language for typed control messages, watchers, and multi-agent routing.               |
| durable execution      | A run model in which state is checkpointed so work can pause and resume without losing progress.                                                             | This is the clearest external name for the wait/watch/recovery behavior Blackdog wants.      |
| checkpointer           | A persistence component that records resumable execution state.                                                                                              | Blackdog needs something morally equivalent for runs and waits.                              |
| interrupt              | A deliberate pause point that saves state and waits for external input before resuming.                                                                      | This is a useful term for approval gates, human review, and takeover points.                 |
| idempotency            | The property that retrying an operation has the same effect as doing it once.                                                                                | Essential for cleanup, retries, landed-state reconciliation, and resumed waits.              |
| provenance             | Where a piece of information or work came from.                                                                                                              | Blackdog needs provenance for prompts, tool calls, results, and landed commits.              |
| lineage                | The chain linking task, attempt, prompt, workspace, branch, result, and commit.                                                                              | This is the audit trail Blackdog currently only approximates.                                |
| roots                  | In MCP, the filesystem boundaries a client exposes to a server.                                                                                              | Important if Blackdog becomes an MCP server or client around worktrees.                      |
| host / client / server | In MCP, the host owns user/session policy, clients maintain server connections, and servers expose resources, prompts, tools, and tasks.                     | Blackdog should be precise about which role it is playing in a given deployment.             |
|                        |                                                                                                                                                              |                                                                                              |

## Comparative Landscape

This comparison is based on primary documentation reviewed on April
10, 2026. The point is not to imitate any one system wholesale. The
point is to name the patterns that already exist in adjacent tools so
Blackdog can adopt the right terms and primitives.


### Repo-native coding agents

#### Aider

[Aider](https://aider.chat/docs/) is a git-native coding assistant optimized for same-repo editing. Its strongest ideas are:

- tight git integration: it commits edits automatically, offers `/undo`, and isolates preexisting dirty changes before it edits
- a `repo map`, which is a compact symbol-level summary of the repository sent with each request
- explicit chat modes, including an `architect` mode that separates planning from final editing

The key lesson from Aider is that code orientation and git hygiene are
first-class product features, not secondary conveniences. Blackdog
should borrow the emphasis on repo maps, commit-linked editing, and
protecting dirty local work. Blackdog should not stop there, because
Aider is still primarily a single-session coding assistant rather than
a durable multi-agent coordination system.


#### OpenHands

[OpenHands](https://docs.openhands.dev/) emphasizes an explicit
runtime with an action/observation loop. Its runtime architecture uses
a client-server pattern where the backend sends actions into a
sandboxed runtime and receives observations back. It also pushes
repo-specific guidance into project skills that are loaded
progressively to conserve context, and it can extend itself through
MCP tool servers.


The useful lesson from OpenHands is that runtime mediation
matters. Actions, observations, sandboxes, and on-demand skills are
cleaner concepts than one giant prompt. Blackdog should borrow the
idea of progressive disclosure for repo guidance and the clearer split
between orchestration and execution environments.


#### mini-SWE-agent and the SWE-agent lineage

[mini-SWE-agent](https://mini-swe-agent.com/latest/) and
[SWE-agent](https://swe-agent.com/latest/) are especially relevant for
terminology. They treat the `trajectory` as the main artifact of a
run, provide inspectors for browsing those trajectories, and make
human control modes explicit. mini-SWE-agent intentionally keeps a
completely linear history and exposes `confirm`, `yolo`, and `human`
modes for the same run.

The lesson here is not that Blackdog should copy a bash-only
agent. The lesson is that runs need inspectable histories and explicit
human-control states. Blackdog should adopt the clarity of
`trajectory`, `inspector`, and mode-switching language, then
specialize it around task-aware attempts, worktrees, and landing
semantics.

### Multi-agent runtimes

#### AutoGen

[AutoGen](https://microsoft.github.io/autogen/) is one of the clearest
examples of an explicit multi-agent runtime. It distinguishes agents
from the runtime that manages them, creates agents on demand, routes
messages by type, and uses topics and subscriptions as a pub-sub
layer. Its higher-level `Teams`, `GroupChat`, `Swarm`, and
`HandoffMessage` abstractions show different orchestration styles on
top of the same lower-level runtime.

The strongest lesson for Blackdog is that a multi-agent system gets
easier to reason about once message protocol, lifecycle, identity, and
subscription semantics are explicit. Blackdog should borrow:

- runtime-managed agent lifecycle instead of ad hoc conversational
  ownership
- typed message protocols
- handoff as a first-class operation
- topic/subscription vocabulary for routing and monitoring

What Blackdog should not borrow is the assumption that a shared chat
thread is enough to model repo work. Blackdog still needs task,
attempt, branch, and commit identity that general chat runtimes do not
provide.


### Durable agent workflow systems

#### LangGraph

[LangGraph](https://docs.langchain.com/oss/javascript/langgraph/)
contributes the clearest public vocabulary around `durable execution`,
`checkpointers`, `thread identifiers`, and `interrupts`. Its model is
explicit: save workflow state, resume with the same thread ID, and
ensure replay is deterministic and side effects are isolated or
idempotent.


This is directly relevant to Blackdog’s wait/watch ambitions. Blackdog
should borrow:


- the idea that a run has a durable identity distinct from any single prompt turn
- explicit pause/resume points
- a checkpointing mindset
- the rule that retries and resumption must be idempotent

The important distinction is that LangGraph is a general workflow
system. It does not know what a worktree, landing gate, or
branch-backed attempt is. Blackdog would still need its own
repo-specific semantics layered on top.


#### Temporal

[Temporal](https://docs.temporal.io/) is not an agent framework, but
it is one of the clearest references for long-running reliable
orchestration. The relevant lesson is durable workflows as
infrastructure: state survives crashes, networks fail without losing
the logical run, and waiting is a runtime primitive rather than an
application afterthought.


Blackdog should take the lesson, not the entire platform shape. In
particular:


- waits should be durable, not conversational
- cleanup and retries should be designed for replay
- status should be queryable independently of whether a terminal session is still open

### Protocol and integration standards

#### Model Context Protocol (MCP)

[MCP](https://modelcontextprotocol.io/specification/2024-11-05/index)
is not a backlog system, a workflow engine, or a supervisor. It is a
protocol for letting AI hosts connect to external servers that expose
`resources`, `prompts`, `tools`, and now experimental `tasks`. It
standardizes host/client/server roles, capability negotiation, roots,
and transport rather than repo-specific execution policy.


That distinction matters. MCP can be an excellent integration boundary
for Blackdog, but it does not replace Blackdog’s internal runtime
model. The right lesson is:


- use MCP to expose or consume capabilities cleanly
- do not confuse protocol primitives with product semantics
- map Blackdog concepts onto MCP deliberately instead of leaking raw
  files and ad hoc commands
  

## What Similar Systems Suggest

Looking across these tools, a few patterns show up repeatedly.

### 1. Structural code context matters

Aider’s repo map and OpenHands’ on-demand skills both solve the same
problem: startup context is expensive. Systems that orient the agent
structurally rather than narratively tend to bootstrap faster and
waste fewer tokens.


Implication for Blackdog:

- add a lightweight repo map or internal API map as a first-class artifact
- keep large repo guidance progressively loadable rather than always inlining it

### 2. Execution histories need a canonical artifact

mini-SWE-agent, SWE-agent, and LangGraph all treat execution history
as a first-class inspectable thing rather than a side effect of
logs. They use terms like trajectory, thread, checkpoint, and
interrupt because those give operators something concrete to inspect
and resume.


Implication for Blackdog:

- `TaskAttempt` should become the canonical execution-history object
- attempts should be inspectable independently of chat transcripts
- prompt receipts, tool calls, waits, and final results should all hang off the attempt

### 3. Multi-agent systems work better with explicit routing

AutoGen’s topics, subscriptions, and handoffs show that complex
collaboration becomes easier to control once routing rules are
explicit rather than implicit in shared chat history.


Implication for Blackdog:

- typed control messages should replace today’s mostly freeform inbox
- watchers, supervisors, and takeovers should have explicit routing scope
- worksets could become the Blackdog equivalent of routing domains

### 4. Durable waiting is its own feature

LangGraph and Temporal both make the same point: if a system needs to
pause and resume reliably, waiting must be represented in the runtime
model.


Implication for Blackdog:

- `WaitCondition` should be first-class
- the system should survive terminal exits and process restarts without losing the logical run
- approval, clean-primary gating, landing readiness, and child completion should all be modeled as durable waits

## Research-Informed Adjustments to the Target Model

The comparison above suggests a few concrete refinements to Blackdog’s target language.

- `TaskAttempt` should be described as Blackdog’s task-shaped trajectory artifact.
- `Run` should be described as a durable execution thread over a workset, not just a supervisor invocation.
- `Workset` should be treated as both a planning scope and a routing
  scope. [We must not introduce workset with epic, lane and wave still
  hanging around, we can ask ourselves if we should have something
  that manifests itself in routing and planning, just planning and
  just routing - that might allow us to refer to these things in a
  sensible way, like a plan might just be part of planning whereas a
  lane might have nothing to do with planning - is a workset a DAG? we
  can't introduce too many organizing principles and we certainly can
  only introduce orthogonal concepts for clarity]
- `PromptReceipt` should include the repo-map or code-summary
  artifacts the agent actually saw. [**this is critical** and can
  allow us to confirm that when an agent that follows another in a
  chain that we can confirm that they are seeing the right thing - we
  can let them start assuming they did see the updated code, but the
  prompt receipt should confirm that by adding appropriate git hashes.]
- `WaitCondition` should be designed as an interruptible durable wait,
  not just a poll loop. [This is important because we don't want to
  impose messaging requirements on task executing agents so we need a
  clear way to monitor a set of them that behaves like message passing
  but is more like looking over shoulders]
- A future Blackdog inspector should browse attempts, waits, related
  messages, and results the way trajectory inspectors browse runs in
  SWE-agent-style systems.
  


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

The most important missing concepts are `TaskAttempt`, `Run`,
`PromptReceipt`, `Workset`, `ValidationObligation`, and
`WaitCondition`. Those are the concepts that would make Blackdog much
better at observability, recovery, and multi-phase execution. [I don't
like name `Run` and would prefer something else and don't think we
should necessarily conflate supervisor and direct execution - thinking
that it might start to overlap TaskAttempt, so Run should be what
TaskAttempt is not in terms of runtime state and results or something
like this]

## Durable Facts vs Derived Views

One source of confusion today is that Blackdog mixes durable truth,
mutable runtime state, append-only records, and rendered views in the
same mental bucket. The target model should separate them cleanly.


### Checked-in durable specs

These are reviewable and belong in git when they exist:

- `blackdog.toml`
- optional checked-in plan specs for major work, imported into the
  runtime backlog [I think resources like this should almost certainly
  be part of the package documentation right? To me, the only thing
  blackdog should think about is preserving the history and that might
  be about some kind of compaction step - but I'm not sure where this
  should go, so let's not worry about it for now. One thing is that it
  would be a real bummer to believe that the git repo could be safely
  deleted and rechecked out because you would lose a lot of blackdog
  hisotry - to me, maybe this is our cost+ model where we host
  blackdog work, but for now, maybe we leave it until i do it accidentally.]
  
- checked-in prompt templates or skill policy that shape agent
  behavior [this for sure we need to do but it raises an important
  point - should blackdog manage itself as a skill in a host repo? So
  when blackdog is installed, does it create a skill and "install" it?
  I would probably think that some kind of Q/A between blackdog and
  repo-owner would be best, so that blackdog could create a skill
  directly in the host package rather than be a skill itself. If it
  were a skill it would be like a blackdog skill creator skill that
  you could use to create a new package skill, for example, a skill
  that we could use that would be something like, <pkg-quick> do x -
  that did something very quickly or <pkg-deploy> release new
  version. These skills would be created in context of blackdog, so
  for instance, the release skill might actually not use a worktree,
  but blackdog should still coordinate that - exactly how is more of a
  adapt to the package]

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

[what are events for and what are typed control messages? We should
make sure that run summaries, task-attempt records and structured
results are sufficiently stamped with state to enable real metrics
over time]. Run summary is exactly why we have to change 'Run' because
it is totally ambiguous - it would better be something like:
WorksetSupervisorStatus or something.]

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
- allow important plan specs to be checked in as reviewed artifacts [
  think that this should be contained in repo docs themselves]
- treat the runtime backlog as the imported, actionable execution surface rather than the only place ideas can live

That means the answer to "should the backlog be checked in?" is "not as mutable runtime state." The better model is a checked-in plan spec plus an unversioned runtime execution state layered on top.

## Task Model

Tasks should remain the core executable unit, but the model around them needs to tighten up.

### `TaskSpec`

A task spec should capture:

- stable task identity
- title and intent
- narrow execution slice [i don't know what this is and why we have
  this - it really confuses me ] 
- affected paths and relevant docs
- approvals and risk posture
- explicit dependencies
- invariants that must remain true
- validation obligations the task creates, satisfies, or invalidates
- workset membership

[we have other options here, a task could come with a worktree pointer
or it could come with a shell script for getting setup, it makes sense
to have different kinds of tasks potentially as part of the system
configuration - you don't want agents set to execute work to have to
figure out where they fit into the system, but you do want them to
know where they fit into the system] 


Blackdog already stores part of this, but dependencies, invariants, worksets, and validation obligations are underdeveloped.

### `TaskState` 
[clearly, this is important and we do not want this to necessarily be
different than the task itself at a minimum tasks have to have a very
well defined interface that they support that is narrow and intuitive
for agents] 

Task state should be a derived object, not a bag of writeable status flags. It should answer:

- is the task ready to run
- is it blocked, and by what
- who owns it
- what is the latest active or terminal attempt
- has it landed
- is cleanup still required


[we don't want all of these questions to have to be answered everytime
we start work. our goal is to do work in smaller context sizes and to
do that we need to make sure that we 'pre-clear' tasks. One model
would be to have a very high reasoning model like 5.4 Extra High be a
coordinator of simpler models that are less large and can compute
faster, it is critical that we capture any reasoning that is used to
determine what model a task should be run on and why it was chosen as
well as what was chosen. 

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

This is the missing bridge between a task spec and a coherent git/log
history.

[okay, who is responsible for this? the invoked agent or the invoking
agent?] 

## Task Lifecycle

The most important lifecycle rule to lock down is that tasks and attempts are not the same thing.

Recommended lifecycle model:

- `TaskSpec` is durable planning truth
- `TaskState` is derived runtime state
- `TaskAttempt` carries execution status

[The thing that i think we want to think about is the degree to which
task spec really is a contingent thing and not likely to be attempted
as-is so many times, a likely thing is that something is attempted and
it doesn't work so we remove it and try something else - that said,
the attempt can be okay, but we should be careful about making
analogies to similar but different workflow systems]

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

[Why is landing in state and landed in attempt - is that not
backwards? - not sure what we gain from these two buckets of very
similar looking things]

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

Takeover should preserve attempt history instead of creating
conversational amnesia. A new actor can become responsible for an
attempt, or a fresh attempt can branch from the previous one, but the
lineage must stay explicit.


## Planning Model

The current `epic` / `lane` / `wave` model has value as a view, but it is too weak and too awkward to be the long-term durable planning truth.

Recommended direction:

- keep `epic` as an optional grouping label or workstream label
- replace implicit lane ordering with explicit DAG dependencies
- treat lanes and waves as generated planning views, not canonical identity or dependency storage
- introduce worksets for focused execution scopes such as `current-refactor`, `parking-lot`, or `release-blockers`
- allow optional concurrency groups or serialization hints when a strict DAG is not expressive enough

The current numbering friction is evidence that lanes and waves are being asked to do more than they are good at. A real graph plus generated views is the cleaner model.


[Agree, but don't love your breakdown - we want to understand these
things from a more operational level, what do we need to manage and
execute work, what do we want to be unaffected by execution
coordination - the 'plan' as it were, we don't want extra terminology
for terminology sake so we can get rid of everything if worksets does
everything we need. I don't want any extraneous things here, but I do
think we might need so orthogonalize this]

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
