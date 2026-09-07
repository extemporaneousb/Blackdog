# Environment Independence and Outcome Evidence

Status: implemented and independently reviewed. A1-A8 have the local evidence
listed below. A9 closure is recorded through the final acceptance task's terminal
typed assessment only after canonical landing and exact-commit hosted verification.
[Release acceptance](RELEASE_ACCEPTANCE.md) records fixed code, artifact, and
measurement evidence; delivery receipts carry the current hosted run proof.

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
| A1 | Task, attempt, evidence, and repository-effect boundaries are explicit. Outcome assessment, execution status, and integration stay distinct. | Verified: documented boundaries, strict schema tests, package-boundary checks, and independent architecture review. |
| A2 | Blackdog runs without a repository `.VE`, source checkout, `PYTHONPATH`, or third-party runtime packages. Python requirements and supported platforms are explicit. | Verified locally: reproducible final archive, isolated subprocess acceptance, and configured four-job CI matrix. Hosted proof belongs to A9. |
| A3 | Fresh-repository begin, show, recovery, close, land, and cleanup work with the portable runtime. Explicit Python handlers still work. | Verified: seven final full-lifecycle scenarios, optional Python-handler preservation, existing linked-target tests, and exact cleanup replay after removal. |
| A4 | Product-observed validation binds the tested tree, commands, environment, task, and attempt. Caller assertions cannot impersonate the runner. | Verified: strict provenance/binding and failure/timeout tests, stable replay and indeterminate fault checks, plus exact-archive typed CLI acceptance. |
| A5 | Defined criteria have explicit assessments and provenance; corrections preserve history. Missing assessments are visible. | Verified: 31 evidence tests, immutable definitions, concurrent correction conflicts, terminal assessment and stale-tree checks, and provenance-preserving aggregate reports. |
| A6 | Measurements declare source, units, phase, eligibility, and missingness. Reports compare defined cohorts without counting missing values as zero. | Verified: known-value cohort, missingness, canceled-versus-met, intervention, and repeated-regression fixtures; explicit coverage and percentile limits. |
| A7 | Optimization improves a measured part of the real workflow without weakening validation or acceptance. | Verified: [baseline and final samples](RELEASE_ACCEPTANCE.md#final-workload-measurements), n=7 each; whole scenario 27.8% faster with every measured regression retained. |
| A8 | Evidence and distribution changes preserve history, exclusive ownership, deterministic retries, and recovery after interrupted writes. | Verified: integrated fault/migration suite, independent zero-outside-write containment cases, concurrent invocation/observation checks, and exact old-reader byte preservation. |
| A9 | Every implementation item reaches the recorded target branch and remains usable after landing and cleanup. | Canonical implementation commits are [listed in release acceptance](RELEASE_ACCEPTANCE.md#delivered-work). The final delivery receipt pairs terminal typed assessment with all four exact-pushed-main CI jobs and artifact hashes; configuration alone never closes this criterion. |

## Work and Review Sequence

1. The decision and acceptance matrix landed after both implementation workers
   and the independent reviewer agreed on interfaces.
2. The standalone external harness landed next so the first runtime workflow
   could invoke a reviewed, already-shipped acceptance entrypoint.
3. Runtime independence, optional project environments, stable recovery, release
   packaging, and measured startup changes landed after adversarial review.
4. Typed evidence admission, observed validation, and phase/report contracts
   landed after integration with the runtime and independent fault review.
5. Final acceptance records combined benchmarks, real typed task adoption,
   canonical target verification, and exact-commit hosted release evidence.

The coordinator assigns work and evaluates evidence. Worker agents implement,
test, and execute canonical Blackdog landing. A reviewer must assess changed
contracts and adverse scenarios, rather than infer correctness from test count.
Each delivered item names its acceptance IDs and records changed behavior,
checks, remaining limits, and canonical commit. Configuration, checks, local acceptance,
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
the final whole-lifecycle evidence appears in [release acceptance](RELEASE_ACCEPTANCE.md).
