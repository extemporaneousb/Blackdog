# Multi-Agent Sequential-Execution Research Prompt

Status: reusable research prompt; not a Blackdog product contract

Use this prompt to investigate a repository whose long-running development work
has accumulated rework, oversized context, or a bespoke sequential backlog
protocol. Replace the bracketed inputs before running it.

```text
Research objective

Determine what has happened in [TARGET_REPOSITORY] over its Blackdog task
history, what changed when [SEQUENTIAL_CAMPAIGN] began, why the work remains
expensive, and which mechanisms from current external tools should change
Blackdog. Produce evidence, not a product roundup.

Inputs

- Blackdog repository: [BLACKDOG_REPOSITORY]
- Target repository: [TARGET_REPOSITORY]
- Candidate sequential-plan authority: [PLAN_OR_BACKLOG_PATH]
- Candidate campaign boundary: [DATE, COMMIT, OR FIRST ITEM]
- Delivery mode: [READ_ONLY | COMMIT | COMMIT_AND_PUSH]
- Maximum active agents: 4 total, including the coordinator

Hard constraints

- Inventory actual owners, schemas, commands, events, claims, attempts,
  validations, prompt receipts, worktrees, recovery actions, and current claims
  before proposing architecture.
- Treat target-repository source, Git history, Blackdog control artifacts, and
  canonical documents as separate evidence sources. Do not infer one from
  another.
- Distinguish measured fact, sourced external fact, inference, recommendation,
  and unresolved question.
- Use current primary sources for external research: official documentation,
  source, schemas, CLI help, release notes, or maintainer issue reports. Record
  the version or access date and link the exact source.
- Do not expose private paths, prompt contents, credentials, unpublished
  identifiers, or private repository text in tracked output.
- Do not mutate either repository during research. If tracked artifacts are
  requested, enter the repository's governed task workflow before editing.
- Do not let agents independently edit the final report. The coordinator owns
  synthesis, verification, lifecycle, commit, landing, and push.

Stage 1: parallel read-only research

Spawn three read-only agents with bounded, non-overlapping assignments.

Agent A — task-history forensics

- Reconstruct Blackdog tasks and attempts from the first retained attempt to the
  current snapshot.
- Identify the campaign boundary from evidence rather than assuming the supplied
  date is correct.
- Compare pre-campaign, plan/preflight, and sequential execution phases.
- Measure at least: tasks, attempts, success/abandon/active counts, landing
  counts, elapsed-time distribution, changed-path distribution, repeated
  hotspots, correction/reversal language, residuals, follow-ups, validations,
  prompt sizes, and control-artifact growth.
- Find examples where Blackdog success did not equal package, architecture, or
  product completion.
- Return reproducible commands and source locations, not raw private artifacts.

Agent B — local sequential-protocol audit

- Trace definition -> frontier selection -> dependency and gate resolution ->
  prompt construction -> claim -> task begin -> worktree -> implementation ->
  validation -> critic -> landing -> successor selection.
- Inventory every bespoke skill, script, checker, capsule, document section,
  lease, hash, and manual rule involved.
- Identify which parts are repository policy, which are generic lifecycle, which
  are caches, and which duplicate another authority.
- Measure startup reads, tool output, validation reruns, critic cycles, and
  coordination incidents where possible.
- State what the bespoke protocol improved and what it merely displaced.

Agent C — external mechanisms

- Select systems by architectural archetype, not popularity. Cover at least:
  readable plan/spec authoring; dependency-ready selection and atomic claim;
  bounded or lazy repository context; continuous agent execution; multi-agent
  coordination; and event-derived crash recovery.
- For every system, trace the same scenario:
  prose/Markdown definition -> validation -> ready selection -> concurrent claim
  -> task identity -> bounded context -> required gate -> crash between writes ->
  recovery -> completion -> next item -> operator stop.
- Name the concrete features currently offered.
- Find at least one documented race, stale-state failure, recovery gap,
  instruction-cost problem, or operational tradeoff for each system. Say when
  primary evidence is unavailable.
- Report dependencies, daemon/database, UI, authoring format, offline, privacy,
  vendor-lock, and migration costs.
- Recommend mechanisms to borrow or reject; never recommend wholesale adoption.

Stage 2: synthesis

The coordinator independently verifies material numbers and sources, then writes
one coherent analysis containing:

1. Executive answer: why this problem is common and why it remains difficult.
2. Method and snapshot boundary.
3. Current Blackdog object and authority inventory.
4. Target-repository task-history summary.
5. Before/after phase comparison with measured tables.
6. What improved under sequential work.
7. Where time is actually spent.
8. What breaks down as the campaign ages.
9. External feature comparison with primary-source links.
10. The minimum Blackdog design delta.
11. Whether a compiler is necessary, what it compiles, and what must not become
    a second durable plan/runtime.
12. Markdown representation, prompt budget, context-pull behavior, claims,
    retries, crash recovery, continuous-processing stops, and migration.
13. Optional repository-index design, including freshness and partial-coverage
    semantics; do not make indexing a prerequisite without measured evidence.
14. Falsifiable acceptance criteria and explicit non-goals.

Stage 3: two independent critics

After the first synthesis, run two new read-only critic passes. Reuse agent slots
only after earlier agents finish.

Critic 1 attacks authority, data duplication, concurrency, task identity,
idempotency, crash boundaries, schema migration, evidence truth, and recovery.

Critic 2 attacks Markdown readability, prompt size, context retrieval, CLI
sprawl, operator stops, continuous-loop safety, migration cost, indexing value,
and accidental recreation of a planner, supervisor, daemon, or UI.

For every severity-high finding, the coordinator must record whether the report
accepted, modified, or rejected it and why. Revise the report before delivery.

Required Blackdog design questions

- Can the feature be expressed as a pure projection of tracked Markdown plus
  existing task/attempt/event state?
- Can normal task IDs remain opaque and be minted only when an item opens?
- Can worksets disappear from product ergonomics without coupling this work to a
  risky storage migration?
- Can ready selection, source-item binding, and task reservation converge under
  concurrent starts?
- Can every crash boundary return one exact existing `next_action`?
- Can continuous processing be a host following typed pull actions, with no
  right to future work, no follow lease, and no Blackdog daemon?
- Can a fresh task receive no more than the current item, its direct dependency
  evidence, safe context pointers, verification contract, and exact lifecycle
  action?
- Can a changed future item avoid invalidating active work while a changed
  selected item blocks precisely?
- Can a failed, blocked, or operator-gated frontier stop progress without
  skipping ahead?
- Can the no-index path remain fully functional across mixed-language repos?

Delivery

- READ_ONLY: return the report and the reusable prompt without repository edits.
- COMMIT or COMMIT_AND_PUSH: create the governed Blackdog task first, write the
  prompt and report only in its returned task workspace, run the repository's
  documentation and public-safety checks, obey exact lifecycle actions, land to
  the recorded target branch, and push only that branch when requested.
- Report the snapshot commit, landed commit, target branch, validation evidence,
  and push result. Do not claim that research or software validation proves
  attended physical behavior.
```

