# AdaptivePlotter Sequential Execution Case Study

Status: research report and Blackdog design input; not a shipped CLI, schema, or
implementation plan

Snapshot: 2026-08-26, AdaptivePlotter `main` at `222fbc1`. One later package task
was active during measurement and is excluded from completed-package timing.

## Executive answer

This is not a novel problem. Existing systems separately solve specification
decomposition, dependency graphs, atomic claims, agent loops, repository
context, multi-agent isolation, and durable workflow recovery. The unsolved part
is keeping all of them consistent as the repository changes without creating a
second project-management product or making every agent read the whole campaign.

AdaptivePlotter demonstrates both halves:

- Blackdog successfully owns isolated attempts, prompts, claims, validations,
  landing, cleanup, and recovery.
- The actual sequential graph, package frontier, completion hierarchy,
  capability stops, critic policy, and fast-start context live outside Blackdog
  in a canonical Markdown ledger plus repository-specific skills, checkers, and
  a hash-bound capsule.

The recommended Blackdog feature therefore needs compiler behavior but not a
durable compiled-plan artifact. Blackdog should deterministically project one
readable Markdown item into one bounded task prompt, bind it late to an ordinary
opaque task ID, and discard the projection. Task, attempt, event, validation,
landing, and exact `next_action` state remain the only execution truth.

There should be no follow lease, backlog owner, daemon, UI, agent launcher, or
right to future items. A host may continuously follow Blackdog's exact pull
actions, but every item starts from a fresh observation and a fresh task context.

## Method and limitations

The local analysis used read-only Git inspection, the public
[AdaptivePlotter repository](https://github.com/extemporaneousb/AdaptivePlotter),
its [episode execution ledger](https://github.com/extemporaneousb/AdaptivePlotter/blob/main/docs/EPISODE_ARCHITECTURE_EXECUTION_PLAN.md),
[current evidence](https://github.com/extemporaneousb/AdaptivePlotter/blob/main/docs/CURRENT_EVIDENCE.md),
repo-local workflow instructions, supported Blackdog reporting commands, and
the private Git-common Blackdog control artifacts. Raw control files and local
paths are not reproduced here.

Metrics are a point-in-time reconstruction, not a benchmark. The pre-sequential
phase contains different work from the foundation packages, so timing does not
measure agent quality. Summary-language counts are a rework signal, not a proof
that every matching task was defective. Blackdog completion proves recorded
delivery evidence, not user value or physical behavior.

The phase boundary is the first canonical episode-plan package, `DOC-00`, on
2026-08-23. Two later groups are separated because the plan/preflight work and
the ordinary sequential software packages have different costs.

## What the Blackdog history contains

At the snapshot, AdaptivePlotter's control state contained:

| Measure | Value |
| --- | ---: |
| Worksets | 119 |
| Tasks | 119 |
| Attempts | 120 |
| Successful/landed attempts | 113 |
| Abandoned attempts | 6 |
| Active attempts | 1 |
| Append-only events | 2,458 |
| Worksets containing more than one task | 0 |
| Tasks with Blackdog `depends_on` entries | 0 |
| Tasks with populated Blackdog path scope | 0 |
| `planning.json` size | 452 KB |
| `runtime.json` size | 1.35 MB |
| `events.jsonl` size | 4.94 MB |

The meaningful execution chain is repository -> task -> claim -> attempt ->
prompt/worktree/change/validation/landing -> events and `next_action`. Workset is
a pervasive compatibility envelope, but in this repository it carries no
multi-task planning value. Building a sequential backlog on worksets would put a
new feature on the least useful object in the current model.

### Before sequential work

The 101 attempts that began before `DOC-00` show the cumulative problem:

| Measure | Pre-sequential value |
| --- | ---: |
| Attempts | 101 |
| Successful/landed | 96 |
| Abandoned | 5 |
| Median successful duration | 32.7 min |
| 90th-percentile successful duration | 97.0 min |
| Mean changed paths | 18.8 |
| Attempts changing at least 30 paths | 19 |
| Attempts touching `OperatorWorkspace.swift` | 83 |
| Summaries using correction/replacement language | 58 |
| Attempts recording residuals | 16 |
| Attempts recording follow-up candidates | 7 |

Blackdog made each change recoverable and attributable, but it did not create a
cumulative architectural center. Eighty-two percent of these attempts touched
the same application owner. Broad tasks repeatedly synchronized source, UI,
tests, fixtures, scripts, and documents. One retained redesign attempt spanned
132 paths and was eventually abandoned after the canonical architecture moved
past it. Successful landing was common while supersession and correction were
also common.

This is the important distinction: transactional success kept individual edits
safe; it did not by itself prevent the repository from accumulating competing
owners, stale plans, repeated hotspots, or evidence debt.

The deterioration just before the campaign is more informative than the
all-history average:

| Attempt start period | Attempts | Median paths | 30+ paths | `OperatorWorkspace.swift` touches |
| --- | ---: | ---: | ---: | ---: |
| August 2–10 | 51 | 13 | 9 | 38 |
| August 11–17 | 41 | 10 | 6 | 36 |
| August 18–23 before `DOC-00` | 9 | 27 | 4 | 9 |

Every attempt in the final pre-plan interval touched the monolithic owner, and
almost half crossed 30 paths. That is the clearest measured version of the
repository having “no center.”

### Plan and preflight phase

Eight explicit plan, inventory, prerequisite-correction, baseline, and wave-
coordination tasks all landed. They averaged 10.9 changed paths and touched the
large application owner once. Their median duration was 19.6 minutes, although
the architecture-readiness task took about four hours and dominates the tail.

This phase established a canonical ordered ledger, named dependencies, package
classes, required gates, a completion hierarchy, current-source inventory,
literal-order selection, a physical-work boundary, and a rollback checkpoint.
It also exposed a failed physical baseline without relabeling it as success.

The 12 tasks currently accepted by the canonical ledger—documentation,
inventory, prerequisite corrections, and six foundation packages—recorded 18.24
hours of active attempt time over 71.68 hours of wall clock. All 12 landed; their
median duration was 59.4 minutes, median/mean change radius was 10/11.7 paths,
none crossed 30 paths, and only one touched `OperatorWorkspace.swift`. They
recorded 53 passed and three skipped validation rows.

### Sequential package phase

Six named foundation packages required eight package-directed attempts. One
EA-02B attempt was abandoned before the accepted run. The first EA-03B landing
passed its declared Blackdog validations, but a fresh critic found contract
defects and forced a separate correction. The table below contains the seven
completed runs; the abandoned EA-02B attempt lasted 27 minutes and changed three
paths.

| Package task | Elapsed | Changed paths | Result |
| --- | ---: | ---: | --- |
| Domain-generic episode contracts | 34 min | 11 | landed |
| Plotter episode model contracts | 73 min | 16 | landed |
| Episode store | 104 min | 9 | landed |
| Operation registry, first run | 93 min | 8 | landed, later corrected |
| Recording store | 202 min | 12 | landed |
| Operation registry correction | 147 min | 7 | landed |
| Recording replay | 191 min | 9 | landed |

Across the seven completed runs:

- all landed and none recorded a Blackdog residual or follow-up candidate;
- mean change radius fell to 10.3 paths;
- no run touched `OperatorWorkspace.swift`;
- median duration rose to 104 minutes and the 90th percentile to 202 minutes;
- total Blackdog attempt time was 14.1 hours; and
- captured execution prompts ranged from 9.5 KB to 17.1 KB.

The smaller change radius and avoidance of the monolithic owner are meaningful
improvements. The longer duration is also real. The packages carried deeper
adversarial contracts, serial Swift validation, synchronized documentation and
checkers, multiple worker/critic cycles, and higher proof requirements. The
sequential system improved convergence; it did not make difficult packages
cheap.

The hotspot moved rather than disappearing. All 12 canonical accepted tasks
touched Current Evidence and the execution plan; 11 touched the episode contract
checker, and 10 touched the current architecture document. Git churn over the
canonical campaign split into about 10,000 source lines, 7,100 test lines, 3,500
repository/protocol lines, and 3,000 documentation lines. The new work is more
modular, but synchronized governance is now the dominant repeated conflict
surface.

## What the bespoke sequential system added

The [ordered work ledger](https://github.com/extemporaneousb/AdaptivePlotter/blob/main/docs/EPISODE_ARCHITECTURE_EXECUTION_PLAN.md#work-ledger)
contained 32 active rows at the snapshot: 12 complete and 20 pending. Stable
package keys are distinct from late-minted Blackdog task IDs. The protocol:

1. reconciles any active Blackdog claim before selection;
2. selects the first dependency-ready ordinary row in literal order;
3. stops at attended physical, remote-Git, ambiguous-evidence, or decision work;
4. compiles a package-specific prompt and creates one normal Blackdog task;
5. uses one coordinator with bounded file and authority leases;
6. serializes integration and all SwiftPM validation;
7. requires synchronized ledger, evidence, architecture, and checker changes;
8. runs a fresh read-only critic before landing; and
9. lands and cleans through Blackdog before recomputing the frontier.

A deterministic launch capsule replaced a model-driven full-plan startup read.
It binds Git state, authority hashes, ledger/evidence reconciliation, package
pointers, gates, and live claims. This made startup fail closed and repeatable.
It did not become selection or reservation authority.

The generic value is clear, but the local cost is also clear. The directly
related skills, coordination references, capsule implementation/tests, and
episode checkers total more than 3,100 lines. Not all of that is disposable—much
of the episode contract belongs to AdaptivePlotter—but Blackdog currently has no
generic primitive for the frontier, source-item binding, compact task packet, or
successor action.

Three additional wave/capsule maintenance tasks consumed another 44 minutes and
23 aggregate path touches. This is small relative to implementation time, but it
is pure local orchestration cost that a generic Blackdog projection could remove.

## Where the time goes

Blackdog's task-worktree setup and landing are not the bottleneck. Recent task
setup was measured in seconds, and landing intent through cleanup was generally
single-digit to low-teens seconds. The two-hour packages spend time elsewhere:

- **Authority discovery.** Even with a capsule, the coordinator verifies the
  plan row, evidence, vocabulary, product and architecture constraints, current
  claim, and exact package prompt. A five-session sample used 11–13 inspection
  calls and produced roughly 151–181 KB of tool output before `task begin`.
- **Source ownership.** The agent must locate actual owners and consumers before
  moving authority. Text search is cheap; establishing semantic ownership and
  deletion safety is not.
- **Serial validation.** Shared build artifacts make parallel SwiftPM execution
  unsafe. Quick, focused, documentation, journey, and strict gates are repeated
  after material corrections.
- **Useful criticism.** The first operation-registry task passed five recorded
  gates, yet a post-landing critic found four material ownership and settlement
  defects. The correction was not ceremonial; it consumed another 147 minutes.
- **Synchronized authority.** Source, tests, package manifests, canonical docs,
  Current Evidence, checker constants, capsule fixtures, exact phrases, and
  frontier text must agree in one landing.
- **Coordination recovery.** One recent wave had a foreign-edit ownership race in
  actively leased paths. Reconciliation, rerun validation, and lost/truncated
  output added substantial time.
- **Conversation mechanics.** Large tool output, compaction, and repeated broad
  reads consume context even when Blackdog's generated prompt is bounded.

Blackdog can remove repeated selection and context-routing work. It cannot make
adversarial design, build/test time, or a real critic finding disappear.

## What breaks down over time

1. **Intent and runtime drift apart.** Markdown describes the campaign while
   task state lives elsewhere. Without an explicit binding, a completed row can
   name the wrong task, and a pending row can already have active work.
2. **Landing is mistaken for outcome.** A successful task can still leave a
   package contract incomplete. Natural-language outcomes, declared validations,
   critic acceptance, migration completion, and physical evidence are different
   facts.
3. **Context accretes.** More history produces larger skills, instructions,
   evidence ledgers, prompts, and startup reads. Summaries then become stale
   authority unless they remain derived caches.
4. **Whole-document hashes become brittle.** An unrelated future-item edit can
   invalidate current work. Hashing only selected semantics without retaining
   provenance creates the opposite problem.
5. **The change hotspot absorbs architecture.** Repeatedly editing one large
   owner makes every task cross-cutting and increases merge, validation, and
   cognitive cost.
6. **DAG readiness is not isolation.** Two dependency-ready tasks can touch the
   same files or semantic owner. Absence of an edge is not evidence that
   parallelism is safe.
7. **Repair work disappears from the success rate.** The operation-registry
   correction appears as two successful tasks unless the item-to-run history and
   post-landing critic result are examined together.
8. **Caches and indexes age.** A repository index tied to a discarded task
   worktree or an old commit silently provides incorrect navigation unless
   freshness and coverage are explicit.
9. **Orchestrators grow into products.** A selector tends to acquire leases,
   queues, retry policy, dashboards, agents, and daemons. The resulting second
   runtime must solve the same recovery and migration problems as Blackdog.
10. **Continuous loops overrun authority.** Refeeding a prompt or automatically
    selecting another ready item can skip a blocker, carry stale assumptions,
    or perform physical/remote work without a new authorization boundary.

## What other systems offer

The useful comparison is mechanism by mechanism. Links below are primary
documentation, source, release, or maintainer-issue evidence checked at the
snapshot date; issue links support failure-history claims, not a claim that the
issue remains unresolved.

| System | Current features | Failure or cost that matters here | Blackdog conclusion |
| --- | --- | --- | --- |
| [Backlog.md](https://github.com/MrLesk/Backlog.md) | Repository-local Markdown tasks with acceptance criteria, definition of done, dependencies, references, plans, comments, and summaries; fresh-session task flow; JSON read/integration view | [Release history](https://github.com/MrLesk/Backlog.md/releases) includes duplicate-ID repair, worktree-aware ID allocation, cross-branch hydration, and stale-write fixes | Borrow readable task grammar and just-in-time planning. Keep claims, opaque IDs, attempts, evidence, and recovery in Blackdog rather than Markdown. |
| [Task Master](https://github.com/eyaltoledano/claude-task-master) / [Hamster loop](https://tryhamster.com/docs/taskmaster/automation/loop) | PRD decomposition, dependency-ready `next`, task expansion, repeated agent launch, completion/block markers, interruption resume | Canonical JSON, a broad default MCP surface, mutable progress context, reported [schema-evolution failures](https://github.com/eyaltoledano/claude-task-master/issues/1708), and unattended permission bypass create new authority and safety costs | Borrow bounded next-item looping and stop-on-blocker. Reject JSON authoring, model completion phrases, global permission bypass, and mutable progress as recovery truth. |
| [GitHub Spec Kit](https://github.github.com/spec-kit/) | Spec -> plan -> dependency-ordered tasks -> implement; cross-artifact analysis; convergence appends missing work; workflows include gates, loops, fan-out, and fan-in | Commands, artifacts, skills, extensions, presets, bundles, and integrations form a large configurable prompt ecosystem | Borrow strict extraction, dependency validation, consistency analysis, and a final convergence item. Do not reproduce the ecosystem. |
| [OpenSpec](https://openspec.dev/docs/quickstart) | Readable proposal/spec/design/tasks artifacts, configurable artifact DAG, apply/resume, fresh implementation sessions, and archive into canonical specs | Checkbox progress has no atomic claim, attempt attribution, validation receipt, worktree identity, or crash reconciliation; multiple artifacts can diverge | Borrow readable intent, small artifact dependencies, and archive discipline. Use Blackdog runtime rather than checkboxes for execution truth. |
| [Beads](https://github.com/gastownhall/beads) and [Gas Town](https://github.com/gastownhall/gastown) | Dependency-ready queries, atomic claims, hash IDs, audit trail, formulas/molecules, agents, worktrees, merge queues, watchdogs, and dashboards | A [ready-then-claim race](https://github.com/gastownhall/beads/issues/3570) was reported when selection and claim were separate; Gas Town's broader design adds Dolt/server modes, daemon recovery, role hierarchies, patrol agents, and documented [restart recovery](https://github.com/gastownhall/gastown/issues/1255) complexity | Borrow atomic select-and-reserve, opaque IDs, worktree isolation, and cold-start recovery. Reject the database, daemon, UI, and agent hierarchy. |
| [GitHub Issues](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/adding-sub-issues) and Copilot | First-class sub-issues, hierarchies, [dependency fields](https://docs.github.com/en/issues/tracking-your-work-with-issues/using-issues/creating-issue-dependencies), CLI/API manipulation, natural-language issue trees, and agent assignment | Remote mutable state is excellent collaboration state but is not a local worktree/attempt/evidence transaction | Accept issue links/import as source references. Do not require issue creation or synchronize runtime status bidirectionally. |
| [Aider repository maps](https://aider.chat/docs/repomap.html) | Tree-sitter-derived symbols/signatures, dependency-graph ranking, dynamic token budget, and selectable refresh policy | Maps are partial, may expand when no files are selected, and weaker models can confuse map text with editable code | Borrow a small, revision-keyed context projection. Do not inject an entire semantic index into every task. |
| [Sourcegraph SCIP](https://sourcegraph.com/docs/code-navigation/precise-code-navigation) | Language-neutral index protocol, language-specific compiler/LSP indexers, precise definitions/references/implementations, CI indexing, and search fallback | Precise coverage requires build-aware indexers and current commit coverage; stale or absent indexes fall back to less accurate search | Use optional producers with freshness/coverage receipts. Do not make a compiler index a universal Blackdog dependency. |
| [Claude Code subagents](https://code.claude.com/docs/en/sub-agents), [hooks](https://code.claude.com/docs/en/hooks), and [Ralph](https://github.com/anthropics/claude-code/blob/main/plugins/ralph-wiggum/README.md) | Isolated subagent context, tool restrictions, worktree isolation, deterministic hooks, and repeated stop-blocking loops | Same-prompt repetition needs iteration caps and exact completion promises cannot distinguish success from blocked; human-judgment tasks are explicitly unsuitable | Let the host create fresh contexts and enforce tools. Blackdog continuation must use typed evidence and exact actions, never prompt self-certification. |
| [Temporal](https://docs.temporal.io/workflow-execution) and [Prefect](https://docs.prefect.io/v3/concepts/states) | Durable event replay, idempotent activities, checkpoints, signals/updates, retries, and distinct failed/crashed/cancelled/paused states | They require their own services, workers, schemas, deterministic/retry disciplines, and operational lifecycle | Borrow event-derived recovery, idempotency, explicit operator input, and precise terminal states. Do not add a workflow engine dependency. |

The pattern is consistent: readable plans work well for intent; durable runtimes
work well for claims and recovery; context tools work when revision-keyed and
bounded. Problems recur when one artifact is forced to own all three.

## Recommended Blackdog shape

### Compile transiently, do not persist an executable plan

A deterministic projector is necessary because Blackdog must reject duplicate
keys, unknown dependencies, cycles, unsafe paths, unknown checks, unknown
capabilities, ambiguous fields, and changed selected semantics. It must also
derive the literal frontier and build a bounded execution prompt.

That behavior should remain a pure projection:

```text
tracked Markdown + repository policy + tasks/attempts/events
  -> diagnostics + derived status + frontier + selected item + bounded prompt
```

Do not emit an editable JSON/YAML plan, persist a second status machine, or
compile every future item into tasks. Persist only immutable source receipts and
item-to-task-run bindings when an item is actually reserved.

### Minimal Markdown

```markdown
# Blackdog Backlog

Format: blackdog-backlog/v1

## EA-05C - Bounded incident exporter

### Outcome

Add one reviewable incident-export boundary. It owns no UI or device authority.

### After

- EA-05B

### Read

- `docs/EPISODE_ARCHITECTURE_EXECUTION_PLAN.md#ea-05c`

### Touch

- `Sources/PlotterEpisodeRuntime/`
- `Tests/PlotterEpisodeRuntimeTests/`

### Verify

- QUICK
- STRICT
- INCIDENT

### Requires

- repository-write
```

`Outcome` is the only mandatory item section. `After`, `Read`, `Touch`,
`Verify`, and `Requires` are optional. `Verify` contains repo-configured check
IDs, never arbitrary Markdown shell. `Requires` contains typed capabilities;
unknown capabilities stop. No checkbox, task ID, attempt state, active owner,
priority, lease, lane, or evidence result is written back into the file.

### Smallest CLI surface

```text
blackdog backlog show [--file PATH] [--item KEY] [--show-prompt] [--json]
blackdog task begin --backlog PATH [--item KEY] --actor ACTOR --json
```

`backlog show` performs parse, validation, runtime reconciliation, frontier and
blocker display, selected-item preview, and generated-prompt byte reporting. It
is read-only.

`task begin --backlog` selects or verifies one item and enters the existing
task-begin reservation/worktree flow. It replaces a caller-authored execution
prompt with the deterministic selected-item packet. Existing `task show`,
`recover`, `land`, `close`, and `cleanup` remain the lifecycle surface.

Do not add `backlog create`, `compile`, `next`, `run`, `follow`, `context`, or a
generic `reconcile` command in v1. Prose/TODO conversion remains a draft produced
by the existing managed skill and requires explicit review.

### Core objects and authority

- **Task:** one late-minted opaque unit of executable intent. Product output
  shows the stable backlog key separately.
- **Attempt:** one actor, prompt receipt, worktree, change set, validation set,
  landing result, and terminal status.
- **Event:** append-only mutation and recovery history.
- **NextAction:** the sole exact continuation/recovery authority.
- **Backlog source:** tracked human intent, stable logical keys, literal order,
  context pointers, checks, and capability requirements.
- **Backlog binding receipt:** source path/blob provenance, item key, selected
  semantic-closure digest, projector version, prompt hash, and an ordered list
  of normal task runs. At most one run is active.

Workset remains a private compatibility envelope in the first release, never an
authored or visible backlog object. Product-level removal can happen immediately;
physical flattening of planning/runtime storage is a separate migration.

An item may need an explicit correction or retry task after a landed run, as
EA-03B did. The binding therefore cannot be permanently one item -> one task.
It is one item revision -> ordered task runs, with no automatic retry and at
most one active run. Dependencies become satisfied only when declared gates and
any explicit convergence item say so; a landed run alone is insufficient.

### Reservation and drift

Ready selection, source-item binding, and task reservation form an idempotent
saga using the existing planning-before-runtime lock order. Retries with the
same inputs converge on the same binding and task. Every retained boundary—
selection request, decision, prompt receipt, envelope, worktree, attempt, and
event append—must return one exact recovery action after a crash.

Record the whole Git blob as provenance, but block admission only when the
selected item's normalized semantics, dependency closure, required capabilities,
or check identities change. Formatting and unrelated future-item edits do not
invalidate active work. Changes to an active item fail closed.

### Short prompts and pull context

Blackdog should cap only its generated execution payload, not claim a total model
context limit. A reasonable first target is 8 KiB UTF-8. The protected kernel
contains the item key, exact outcome/acceptance text, dependency evidence used
for admission, capability stops, named checks, `Read`/`Touch` pointers, task
identity, and exact lifecycle rule. It never truncates that kernel; oversize
items fail with a split diagnostic.

The agent pulls actual files and symbols on demand using repository-native tools.
The full backlog, evidence ledger, routed documents, and coordination protocol
are not injected into every task.

### Continuous processing without a follow lease

Continuous mode belongs to the host following Blackdog's pull protocol:

```text
observe -> begin one item -> fresh agent context -> terminal task operation
  -> project current backlog again -> exact next begin action or typed stop
```

Blackdog never reserves a future item, owns an agent session, carries credentials,
or loops internally. After complete landing/finalization/cleanup, `next_action`
may expose the exact guarded `task begin --backlog ...` command. A host may open
a fresh context and execute it. Another caller may win the next reservation;
the old host then observes the real owner and does not skip ahead.

Failed, abandoned, drifted, dependency-blocked, physical, credential,
deployment, remote-write, or ambiguous-evidence items stop literal-order
continuation. Bounds on item count, elapsed time, cost, and retries belong to the
host authorization, not a durable lease.

### Optional language-neutral repository index

Indexing is not a v1 prerequisite. Explicit pointers, Git, `rg`, Markdown
headings, build metadata, and exact task history must remain sufficient.

If startup measurements still justify an index, store a derived, disposable
repository graph under Blackdog's Git-common control directory:

- files: path, Git blob, kind/language, generated/vendor/test/source/doc/config,
  package/module, and size;
- fragments: Markdown headings/spec items, declarations, tests, build targets,
  config sections, and content hashes;
- edges: defines, references, imports, depends-on, tests, documents, configures,
  touched-by-task, and validated-by, each with producer and confidence;
- receipts: indexed tree, producer/version, coverage, failures, and freshness.

Built-in producers cover Git, Markdown, configuration, build manifests, exact
text, and Blackdog task/change history. Optional language adapters may consume
SCIP, a compiler, or an LSP for Swift, Python, TypeScript, Rust, C/C++, and other
languages. A rename-aware Git diff deletes removed records, moves renames,
reparses changed blobs, refreshes only affected semantic units, appends task
touches, and transactionally publishes a new snapshot. A task worktree uses the
landed-tree snapshot plus its dirty diff overlay.

Index output is advisory context. Partial or stale coverage is visible, and it
can never change frontier selection, capability admission, checks, evidence, or
completion.

## Independent critic disposition

Two read-only critics attacked the first synthesis from different directions.
Their severity-high findings changed the recommendation as follows:

- The architecture critic rejected an atomic transaction spanning guards, Git,
  handlers, and runtime locks. The report now requires an idempotent reservation
  saga with exact recovery at every retained boundary.
- Both critics rejected a durable compiled graph and a new `BacklogSpec`
  authority. The report now specifies a pure projection plus minimal immutable
  binding receipts.
- Both rejected whole-file drift binding. Whole-file identity is provenance;
  only the selected semantic closure blocks active work.
- The lifecycle critic used the real EA-03B correction to reject permanent
  one-item/one-task binding. An item revision may have ordered explicit task
  runs, never more than one active.
- Both rejected a follow lease, internal runner, and future-item reservation.
  Continuous work is now explicitly a host behavior over exact pull actions.
- The ergonomics critic collapsed the proposed command family to one read-only
  backlog view plus one `task begin` input mode and set a falsifiable generated-
  prompt budget.
- Both made indexing optional and advisory. The no-index path must remain fully
  functional, and any later provider must expose revision, coverage, producer,
  and failure receipts.
- Both required shadow equivalence and active-claim detection before removing
  AdaptivePlotter's bespoke protocol. The current active package is an explicit
  migration blocker, not a reason to infer or steal its binding.

## Implementation sequence to test the design

1. Instrument current prompt/read/validation cost and add no feature yet.
2. Implement the pure parser/projector and `backlog show`; persist nothing.
3. Add the minimal binding receipt and `task begin --backlog` idempotent saga.
4. Extend terminal `next_action` projection without adding a runner or lease.
5. Shadow AdaptivePlotter's recorded ledger snapshots and prove identical
   frontier, blocker, gate, and physical/remote stop behavior.
6. Adopt existing package/task history only through verified task, commit,
   prompt, and evidence bindings; never infer completion from prose.
7. Run one generic Blackdog-managed package, then delete only the equivalent
   bespoke selector/capsule/protocol behavior.
8. Evaluate a derived repository index separately against measured startup cost.

Do not migrate AdaptivePlotter while its current package task is active.

## Falsifiable acceptance criteria

1. Duplicate keys, missing outcomes, unknown dependencies, cycles, escaping
   paths, unknown checks, and unknown capabilities fail read-only with no state.
2. No future item receives a task ID before reservation.
3. Thirty-two concurrent begins for one item create one binding, one task ID,
   and at most one active attempt.
4. Injected crashes at every reservation/start boundary recover through emitted
   actions without duplicate tasks, claims, worktrees, or events.
5. Formatting and unrelated future-item edits do not invalidate active work;
   selected semantic changes do.
6. Missing, failed, or disallowed-skipped required checks cannot make a
   dependent item ready.
7. A blocked or failed frontier is never skipped by continuous processing.
8. No runtime or event row represents a follow lease, backlog owner, or future
   reservation.
9. Generated prompts do not exceed 8,192 bytes; protected content is never
   truncated.
10. Prompts contain neither the full backlog nor whole routed documents,
    evidence ledgers, or coordination protocols.
11. Public help and normal task output require no workset ID.
12. Existing task recovery and landing actions remain the sole lifecycle
    authority for bound items.
13. A host following exact actions across three safe items creates three tasks
    and three fresh prompts, then stops at a typed operator-gated item.
14. The no-index path passes the same selection, admission, and lifecycle tests
    across mixed-language fixtures.
15. Stale or partial index output changes only context suggestions and always
    reports its coverage.
16. AdaptivePlotter shadow projection detects an existing active package task
    and refuses to create a duplicate.
17. Blackdog's full tests, concurrency/fault tests, CLI smoke tests,
    documentation checks, and public-safety check pass.

## Bottom line

AdaptivePlotter did not need a better UI or a bigger supervisor. It needed a
small missing join between readable long-range intent and Blackdog's already
strong task runtime. Implement that join as a transient projection and a late
binding receipt. Keep the continuous loop in the host, keep repository context
pull-based, and measure whether indexing saves enough startup time before making
it part of the product.
