# Product Spec

This document answers the question the architecture docs should not answer:
what Blackdog needs to do to be usable.

Use this document to decide:

- which workflows Blackdog v1 must support
- which legacy surfaces should be kept, changed, combined, deferred, or removed
- what telemetry and stats are required for dogfooding in real repos

Do not use this document as the storage or package-boundary reference.
Use [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for that.

## Product Position

Blackdog is a repo-scoped planning and execution memory system for
AI-assisted local development.

Humans should use Blackdog to:

- express goals
- approve or redirect work
- inspect progress and outcomes

Agents should use Blackdog to:

- shape work into worksets and tasks
- execute kept-change tasks through the WTAM worktree lifecycle
- record prompts, results, and runtime evidence
- expose status and history back to humans

Blackdog also needs repo lifecycle workflows that are not themselves workset or
task mutations:

- install or update Blackdog in a repo
- refresh or regenerate repo-local skill/scaffold surfaces
- tune or preview prompt/skill composition against the repo contract

## Locked V1 Decisions

These decisions are no longer open:

- claims attach to both worksets and tasks
- the first-class execution models are `direct_wtam` and `workset_manager`
- completed and landed work stays in durable history instead of being collapsed
  into only current status
- non-worktree execution is not part of Blackdog's product model

## The Missing Product Artifact

The repo already has:

- a charter: why Blackdog exists
- a target model: the durable object model
- an architecture doc: where code and storage ownership belongs

What it was missing is a product spec:

- what the product must let a human and an agent do together
- what counts as v1
- what is explicitly not in v1

## Users

### Human Operator

Owns goals, approvals, redirects, and release judgment.

### Direct Agent

Runs in the same thread/session as the user and uses Blackdog to shape work,
pick work, and record results.

### Coordinating Agent

Optional higher-level agent that supervises or steers more than one task
attempt.

## Desired Blackdog Functionality

Blackdog is usable when it reliably supports these jobs:

1. Turn a repo goal into a bounded workset with executable tasks.
2. Tell an agent what is ready now and what is blocked.
3. Start execution in a way that preserves worktree, branch, and commit
   identity.
4. Capture prompt/input lineage before and during each attempt.
5. Record outcomes and runtime stats after each attempt.
6. Summarize status, progress, and recent results for a human.
7. Let a human redirect or reshape work without losing history.
8. Recover after interruption without forcing the user to reconstruct state
   from chat logs.

It is also usable when it supports these repo lifecycle jobs without pretending
they are task execution:

1. install or update Blackdog in a target repo
2. refresh repo-local skill or scaffold surfaces after a package change
3. preview or tune prompt/skill composition before execution

## Workflow Families

Blackdog has two different workflow families.

### 1. Workset Execution Workflows

These workflows operate on durable planning/runtime state:

- workset/task shaping
- ready-task selection
- WTAM claim/start/land/cleanup
- status, snapshot, recovery, and result history

These belong to the typed workset/task model and are represented in
`planning.json`, `runtime.json`, and `events.jsonl`.

### 2. Repo Lifecycle Workflows

These workflows operate on the repo's Blackdog installation, skill contract,
and prompt-composition surface:

- install
- update
- refresh/regenerate
- tune/preview prompt composition

These are first-class product workflows, but they are not themselves
worksets, tasks, claims, or attempts. They belong in the product layer and
should surface through explicit CLI and skill workflows rather than being
encoded as planning state.

## V1 Stories

These stories define the v1 target.

### Story 1: Shape Work From A Real Goal

Human:
“Take the test stabilization work in this repo and shape it into something an
agent can execute.”

Blackdog must support:

- one workset for that deliverable
- a task DAG inside that workset
- scope, docs, paths, checks, and branch intent
- a status view that shows the newly shaped work

This is the intake story. If this is clumsy, Blackdog will not get used.

### Story 2: Ask What To Do Next

Human or agent:
“What is the next task I should do in this workset?”

Blackdog must support:

- ready-task selection from typed state
- explicit blocked reasons for tasks that are not runnable
- workset-bounded visibility by default

This is the minimum operational read path.

### Story 3: Execute Kept Changes Safely

Human:
“Start the next task and do the work in the correct workspace context.”

Blackdog must support:

- workset claims and task claims
- one preview surface that shows the WTAM start plan before mutation
- task execution state
- canonical exported workspace identity
- actual worktree path and role
- branch and start-commit identity from the executing checkout
- target branch / integration branch intent
- prompt receipt capture at execution start
- repo contract inputs visible before execution start
- worktree-local CLI setup so the task worktree can run Blackdog directly
- enough attempt identity that later stats, summaries, and prompt review make sense

`blackdog worktree start` is both the operator-facing claim action and the
execution start action for `direct_wtam`.

For v1, the kept-change path should be one operator workflow:

- `blackdog worktree preflight`
- `blackdog worktree preview`
- `blackdog worktree start`
- do the work inside that task worktree
- `blackdog worktree land`
- `blackdog worktree cleanup`

### Story 4: Record Results And Stats

Agent:
“I finished this slice; here is what changed and what I verified.”

Blackdog must support recording:

- task and workset identity
- actor identity
- start/end time
- elapsed duration
- workspace, worktree, and branch identity
- prompt receipt and prompt hash
- start commit
- changed paths
- validation commands and outcomes
- result status
- residual risks or follow-up candidates
- commit or landed-commit linkage when present

This story matters because Blackdog is not just task selection. It is also how
you want to accumulate operating data from real repo work.

### Story 5: Human Asks For Grounded Status

Human:
“Where do things stand right now?”

Blackdog must support:

- a concise human-oriented summary
- a machine-readable snapshot
- recent results and current blockers
- counts that match durable runtime state

If this story fails, the product stops being trustworthy.

### Story 6: Redirect Or Replan Without Losing Lineage

Human:
“Stop doing that task, split this one, and point the workset at a different
integration branch.”

Blackdog must support:

- typed workset/task mutation
- explicit runtime updates
- preserved event history

This story is required for the product to be steerable rather than just a queue.

### Story 7: Resume After Interruption

Human:
“The agent stopped. What was in progress, what is blocked, and what should
happen next?”

Blackdog must support:

- durable execution state
- result and event inspection
- enough state to continue without reconstructing context from chat

This is essential for real-world dogfooding.

### Story 8: Refresh A Repo After Blackdog Changes

Human:
"I updated Blackdog. Refresh this repo so the local skill and managed contract
surfaces match the current package."

Blackdog must support:

- repo-local install/update/refresh behavior
- clear knowledge of which repo-managed files are in scope
- skill/scaffold regeneration without confusing that operation with task
  execution

This is a repo lifecycle story, not a workset/task story.

### Story 9: Inspect Or Tune The Composed Prompt Surface

Human:
"Show me the prompt/skill context Blackdog would use, and help me tune it."

Blackdog must support:

- prompt/skill preview without starting task execution
- the ability to include or omit expanded skill text
- prompt shaping/tuning against the repo contract

This is a first-class operator workflow. It should not be forced through task
claims or attempt history unless execution actually starts.

## V1 Feature Set

V1 should include these product capabilities:

- typed workset/task planning
- ready-task selection
- mutable task runtime state
- explicit workset/task claims
- worktree-backed WTAM preflight/preview/start/land/cleanup
- prompt receipt capture
- prompt/contract preview before execution start
- result/stat recording
- human summary/status
- machine snapshot export
- typed replan/update of workset and task state
- interruption-safe state recovery

Blackdog should also keep a first-class repo lifecycle family in scope:

- repo install/update/refresh workflows
- prompt/skill preview and tuning workflows

## Keep / Change / Combine / Defer / Remove

This is the decision frame for the rest of the repo.

### Keep Now

- `planning.json`
- `runtime.json`
- `events.jsonl`
- workset/task typed model
- workset/task claim model
- `worktree preflight`
- `worktree start`
- `worktree land`
- `worktree cleanup`
- `summary`
- `next`
- `snapshot`

### Keep With Changes

- result recording:
  keep the capability, but align it to the new attempt/runtime model and stats
  contract
- worktree-aware execution:
  keep the capability, but make actual git worktree identity part of the
  attempt record instead of treating it as optional context
- prompt shaping and prompt reuse:
  keep the capability, but ground it in stored prompt receipts and attempt
  history instead of ad hoc chat memory
- repo lifecycle workflows:
  keep install/update/refresh/tune as first-class workflows, but rebuild them
  as explicit repo lifecycle surfaces in the product layer rather than as task
  or workset operations
- supervisor/status:
  keep only if it reads and writes the new typed runtime state directly and
  uses `workset_manager` claim/execution semantics

### Combine

- claim + execution start are one operator-facing action in `direct_wtam`
- result record + land may become one finish/report action
- summary + next may remain separate commands but should read from one status
  model

### Defer

- static HTML board
- threads/conversation management
- tracked installs and multi-repo observation
- browser write UI
- richer multi-agent steering if it delays direct-mode usability

### Remove

- markdown backlog parsing as canonical logic
- durable `epic`, `lane`, and `wave`
- any surface preserved only for legacy compatibility

## Required Stats For Dogfooding

If Blackdog is going to be useful again in other repos, v1 needs a small but
real telemetry contract.

Minimum per-attempt stats:

- `workset_id`
- `task_id`
- `attempt_id`
- actor
- model / reasoning mode when known
- started_at / ended_at
- elapsed_seconds
- workspace identity
- worktree role / worktree path
- branch / target branch / integration branch
- start_commit
- execution_model
- prompt_hash
- changed_paths
- validations and statuses
- result status
- landed commit when applicable
- residuals / follow-ups

Without this, the product may coordinate work, but it will not capture the
operating data you explicitly want from real usage.

## Suggested V1 Command Surface

The exact names can change, but the product should expose capabilities in this
shape:

- one planning write surface for workset/task updates
- one WTAM lifecycle surface for `direct_wtam`
- one supervisor/workset-manager surface for multi-agent workset execution
- one repo lifecycle surface family for install/update/refresh/tune and skill
  composition
- one human summary surface
- one machine snapshot surface
- one ready-task selection surface

The current minimal slice already covers part of this. The remaining work is to
fill in richer replan and recovery behavior against the new model rather than
reviving the old command tree wholesale.

## Release Criteria For “Usable Again”

Blackdog is usable again when you can dogfood it in another repo for a real
direct-agent workflow:

1. shape a workset from a real goal
2. ask what is next
3. execute at least one kept-change task with explicit worktree/git identity
   and a stored prompt receipt
4. land it through the primary checkout and clean up the task worktree
5. record result and stats
6. ask for status after one or more tasks
7. survive at least one interruption and continue from durable state

Supervisor-led multi-agent execution is valuable, but it should not block that
direct-mode usability target.
