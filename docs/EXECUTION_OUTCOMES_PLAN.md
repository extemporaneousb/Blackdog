# Environment Independence and Outcome Evidence

Status: accepted implementation plan. The acceptance criteria below remain open
until implementation, independent review, and final target-branch verification
provide the listed evidence. This document does not introduce shipped commands.

## Decision

Blackdog should make agent work cheap to start, safe to finish, and measurable
by the outcome it produces. Its core consists of three related contracts:

- **Task:** one executable intent, optionally defined by immutable criterion
  identities and a comparison class.
- **Attempt:** one execution with exclusive ownership, provenance, terminal
  execution status, and separate integration evidence.
- **Evidence:** a typed observation or assertion, its subject, source, and
  applicability. Evidence supports an outcome assessment without redefining
  execution success or canonical landing.

`blackdog_core` owns identities, strict records, transitions, append-once
evidence, and derived views. `blackdog` performs Git, environment, validation,
and repository-policy effects. `blackdog_cli` remains an adapter. No scheduler,
transcript engine, dashboard, automatic task selection, or second task store is
needed for this milestone.

Blackdog's executable must be independent of repository `.VE` availability.
Explicit project environment handlers remain supported. Consumers use a stable
release artifact; source development may execute task-worktree code while
recovery resolves an executable that survives cleanup. The initial portable
Python artifact requires a compatible Python interpreter; bundled interpreters
or a native-language port require separate evidence and decisions.

New outcome evidence belongs in strictly versioned event families in the
existing canonical `events.jsonl`. `runtime.json` remains schema 4 and the only
mutable lifecycle authority. Existing runtime and event bytes are preserved;
older tasks acquire no invented criteria, acceptance, or timings. Existing
schema-3 migration remains explicit and digest guarded.

The initial task definition is immutable. Assessment corrections append a
record naming the exact predecessor for the same task, definition, and
criterion. Conflicting predecessors fail under the event lock. Event order and
identity determine succession; timestamps do not break conflicts. Admission
validates real task and attempt identity with consistent lock ordering.

Caller declarations, caller-recorded reviewer assertions, and product-observed
validation results remain distinguishable. Naming a reviewer does not
authenticate that person's acceptance. Only Blackdog's validation runner emits
machine-observed validation evidence. Receipts bind the task and attempt,
tested source tree, command-set digest, environment descriptor, result, and
timing. An unchanged-tree check follows execution. Historical or stale results
remain visible with explicit applicability; they cannot attest to a new tree.

Validation invocation intent precedes execution and uses a stable run identity.
A completed retry returns the recorded result without rerunning commands. A
crash between command execution and receipt persistence leaves an indeterminate
run; a deliberate new run identity is required to execute again. Append-once
storage does not establish exactly-once external command execution.

This milestone does not add validation caching or change landing admission
policy. Existing `--validation` rows remain caller declarations. Typed outcomes
and observed validation add trustworthy assessment and reporting without
granting a second source of lifecycle execution authority.

## Acceptance Matrix

| ID | Required behavior | Completion evidence |
| --- | --- | --- |
| A1 | Task, attempt, evidence, and repository-effect boundaries are explicit. Outcome assessment, execution status, and integration stay distinct. | This decision, strict schema tests, package-boundary tests, and independent architecture review. |
| A2 | Blackdog runs without a repository `.VE`, source checkout, `PYTHONPATH`, or third-party runtime packages. Python requirements and supported platforms are explicit. | Reproducible artifact build; isolated subprocess checks including paths with spaces; CI build/test configuration; actual CI results reported separately from local results. |
| A3 | Fresh-repository begin, show, recovery, close, land, and cleanup work with the portable runtime. Explicit Python handlers still work. | End-to-end subprocess scenarios, existing-environment preservation, linked-target-branch checks, and executable exact recovery commands after worktree removal. |
| A4 | Product-observed validation binds the tested tree, commands, environment, task, and attempt. Caller assertions cannot impersonate the runner. | Reject forged provenance and stale bindings; command failure/timeout cases; unchanged-tree checks; completed replay without reexecution; indeterminate-run crash tests. |
| A5 | Defined criteria have explicit assessments and provenance; corrections preserve history. Missing assessments are visible. | Definition/assessment round trips, identity and supersession conflict tests, malformed/unknown-version rejection, and separate execution/integration/outcome report examples. |
| A6 | Measurements declare source, units, phase, eligibility, and missingness. Reports compare defined cohorts without counting missing values as zero. | Known-value fixtures for sample, eligible, and missing counts; documented percentile method; task versus attempt denominators; retry, intervention, acceptance, and regression coverage. |
| A7 | Optimization improves a measured part of the real workflow without weakening validation or acceptance. | Comparable baseline/candidate fresh-repository lifecycle runs, sample counts and conditions, phase timings, observed changes, and limitations. Help latency alone is insufficient. |
| A8 | Evidence and distribution changes preserve history, exclusive ownership, deterministic retries, and recovery after interrupted writes. | Migration fixtures, event conflict/corruption tests, crash injection at changed persistence boundaries, concurrent unrelated-task preservation, and old-reader evidence preservation. |
| A9 | Every implementation item reaches the recorded target branch and remains usable after landing and cleanup. | Worker check results, independent review findings and resolutions, canonical commit/ancestry proof, clean target checkout, rebuilt artifact, and post-land CLI acceptance. |

## Work and Review Sequence

1. Land this decision and acceptance matrix after both implementation workers
   and the independent reviewer agree on the interfaces.
2. Implement runtime independence, optional project environment handling, stable
   recovery command resolution, and tested release packaging. Review and land
   this item before the evidence implementation.
3. Implement typed evidence admission, outcome assessment, observed validation,
   and phase/report contracts. Work may proceed in a separate task workspace
   after interface agreement. Review against the landed runtime implementation.
4. Complete integrated fault tests, comparable lifecycle benchmarks, measured
   optimization, and independent acceptance. Return concrete defects to the
   owning worker until resolved; then land and verify the final target state.

The coordinator assigns work and evaluates evidence. Worker agents implement,
test, and execute canonical Blackdog landing. A reviewer must assess changed
contracts and adverse scenarios, rather than infer correctness from test count.
Each item names its acceptance IDs and records changed behavior, checks,
remaining gaps, and canonical commit. Configuration, checks, local acceptance,
and actual CI execution are separate evidence claims.

## Measurement Rules and Baseline

Compare equivalent task classes, definitions, command sets, runtime modes, and
relevant environment conditions. Report sample sizes and the population eligible
for each metric. Missing timings, interventions, costs, or assessments remain
missing. Do not infer waiting time by subtracting overlapping phase durations,
or infer human involvement from arbitrary retry counts. Acceptance coverage and
regressions derive from criterion assessments with stated provenance. Report
p50 and p95 with a named method and a small-sample caveat where applicable.

Baseline commit: `605991856e54cbf34804365e241c4455ca6c09ec`.
`make test` passed all 111 tests in 128.372 seconds, including public-check over
58 candidate files. This verifies the existing suite, not the new criteria.
The baseline has no CI workflow or tested release-artifact surface. Existing
handler and validation command durations provide reusable instrumentation;
whole-lifecycle benchmark evidence will accompany implementation acceptance.
