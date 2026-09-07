# Outcome Evidence

Blackdog makes work cheap to start, safe to finish, and measurable by its
recorded outcome. Tasks own intent, attempts own execution and integration,
and typed evidence describes what was assessed and measured. Evidence cannot
change lifecycle authority.

## Define and assess an outcome

After `task begin`, record one immutable definition using the returned task and
attempt identities:

```bash
blackdog task outcome --task TASK_ID --attempt ATTEMPT_ID \
  --definition-file definition.json --json
```

```json
{
  "schema_version": 1,
  "task_class": "parser-correction",
  "objective": "Correct parser behavior against the agreed fixtures",
  "criteria": [
    {"id": "fixtures", "description": "All agreed parser fixtures pass", "required": true}
  ]
}
```

The actor must own the active attempt. Definitions require at least one required
criterion and one to 64 unique criterion IDs. The returned
`definition_sha256` identifies the entire definition. An exact replay is a
no-op; changing the definition is refused. Schema versions describe record
formats and do not provide a definition revision mechanism. A changed goal
requires a new task.

Run the configured commands in the recorded task worktree:

```bash
blackdog task validate --task TASK_ID --attempt ATTEMPT_ID \
  --run-id validation-1 --json
```

A stable run ID identifies an invocation, not a validation cache entry. Blackdog
records its intent durably before executing commands. A completed retry returns
the same receipt without executing again. An intent without a durable result is
`indeterminate`: the process might have executed, and a retry cannot infer its
result. The same run ID does not run again. An operator can choose a new run ID
to deliberately perform a new invocation. This protocol does not promise
exactly-once external effects across a process crash.

The receipt includes per-command status, exit code, monotonic duration, output
byte counts and command SHA-256. Command text and output are not retained. The
binding identifies the source commit, effective Git tree, command hashes and
order, timeout, and bounded runtime descriptor. A private temporary Git index
captures tracked and untracked content under normal ignore rules without
changing the user's index. Git may write content-addressed objects during this
observation. Validation commands run through the existing shell runner.

Before and after binding checks expose `current`, `stale`, or `unknown`
applicability. `current` means the observed inputs match; it does not establish
hermetic execution. The descriptor covers the Python runtime, platform,
configured handlers and Blackdog distribution identity. A running archive is
identified by its SHA-256. Source execution uses the SHA-256 of the release
builder's deterministic distribution projection: Git-indexed or installed
manifest files, with the same inclusion and containment checks. This includes
the shipped runner and core semantics; arbitrary untracked importable source
is outside that projection. Unavailable identity remains unknown and cannot
qualify a receipt or a comparable cohort. Ambient environment
values, external dependencies and services remain unobserved. Changed observed
inputs are stale, and an unavailable worktree or post-run binding is unknown.
The command exits nonzero for failed commands, indeterminate invocations, or
non-current applicability. A successful exit is a bounded machine observation.

After implementation or landing, record an assessment:

```bash
blackdog task outcome --task TASK_ID --attempt ATTEMPT_ID \
  --assessment-file assessment.json --json
```

```json
{
  "schema_version": 1,
  "assessment_id": "review-1",
  "definition_sha256": "DEFINITION_SHA256",
  "criterion_id": "fixtures",
  "result": "met",
  "evaluator": "reviewer",
  "evaluator_kind": "human",
  "provenance": "reviewer_asserted",
  "evidence_refs": ["VALIDATION_RESULT_EVENT_ID"],
  "supersedes": null
}
```

Use actual hashes and event identities in place of the example placeholders.
Results are `met`, `not_met`, or `not_assessed`. Evaluator kinds are `agent`,
`human`, or `external`; provenance is `caller_declared` or `reviewer_asserted`.
The recording actor must own the selected attempt. A reviewer assertion is
caller-recorded evidence: **Blackdog does not authenticate the evaluator's
identity or prove that a human performed the review.** Caller input cannot
select machine validation provenance.

References are optional for a declared assessment. Each supplied reference
must resolve to a completed machine validation from the same task, attempt and
source tree, with unchanged observed inputs during execution. A `met`
assessment cannot cite failed commands. An assertion without references remains
an assertion. It is never promoted to machine-proven acceptance.

A correction uses a fresh `assessment_id` and names the current criterion head
in `supersedes`. Admission compares that predecessor under the canonical event
lock; two concurrent corrections cannot both replace the same head. All prior
assessments remain available. A `met` to `not_met` correction is an explicit
assessment reversal; it does not establish a production incident.
Reports count distinct criteria with these corrections separately from
transition observations. Repeated toggles do not inflate the distinct count.

Review can occur after landing. A terminal assessment binds the durable source
commit's tree and does not reopen the task or rewrite the attempt. Reports
require current required assessments for the latest attempt and final source
tree before reporting the caller-assessed outcome as `met`. Missing criteria,
an old attempt, changed final content, or an unavailable final source identity
produce `not_assessed`.

**Outcome and validation receipts do not authorize landing.** This milestone
adds no receipt gate, validation cache, or new repository policy.
`task land --validation NAME=passed` remains caller-declared evidence under the
existing guards and landing contract. Integration uses the separate source and
landed commit identities. A machine command pass, caller-assessed outcome and
landed change remain distinct facts.

## Record interventions and inspect results

Explicit intervention observations use the same owned attempt without
reopening its lifecycle:

```bash
blackdog task outcome --task TASK_ID --attempt ATTEMPT_ID \
  --measurement-file intervention.json --json
```

```json
{
  "schema_version": 1,
  "measurement_id": "intervention-1",
  "metric": "human_interventions",
  "unit": "count",
  "value": 1,
  "provenance": "caller_declared",
  "reason": "A reviewer supplied a missing acceptance decision"
}
```

Each ID records one immutable incremental observation. Exact retries do not
add to the count. Values are nonnegative integers; boolean and nonfinite
values are invalid. Missing observations produce null, not zero. Recorded
counts do not prove complete coverage of human interventions.

```bash
blackdog task outcome --task TASK_ID --json
blackdog stats --project-root . --no-codex --json
```

The outcome command returns task detail. Both `task outcome` and
`task validate` always emit JSON; `--json` makes this
default explicit. Outcome's `--attempt` and `--actor` apply only when recording.
Stats returns compact outcome cohorts
without provider-history reads or imports when `--no-codex` is supplied.
Provider counters are null with `not_requested` missingness. Ordinary stats
retains its existing provider reporting. Outcome metrics themselves never
mine conversations or use optional lifecycle observability as completion or
retry evidence.

Cohorts match declared task class, full definition SHA-256 and one validation
command-set identity, environment fingerprint, and environment coverage.
Multiple command sets, mixed or unknown environment identities, or missing definitions are marked
non-comparable. The full definition includes objective text: task-specific
objectives deliberately split cohorts, often into single samples. Reusable
definitions can share fingerprints; a shared class alone does not establish
comparability. These identities establish comparable recorded inputs, not
randomized experimental equivalence; hardware/load and unobserved dependencies
can still differ. Each cohort exposes task/attempt counts and timing sample
counts, missingness, median, and nearest-rank p95. The `small_sample` flag
identifies fewer than 20 observations; p95 for small samples is largely a
maximum and does not establish tail behavior. No significance or
performance-improvement claim is derived automatically.

Compact aggregates and cohorts retain required-criterion assessment coverage
by provenance, historical assertion counts, and outcome provenance counts.
They also retain explicit intervention totals and correction observations,
with task and criterion coverage. When no interventions or assessments are
observed, totals remain null; recorded zero observations do not prove complete
coverage.
Corrections preserve both their earlier assertion and current head without
counting the same criterion twice as currently assessed. Reviewer assertions
remain unauthenticated in every view.

Validation reports label passes as historical command results and separately
count receipt applicability as current, stale, or unknown. Read-only reporting
observes one binding per active clean attempt; dirty, removed, or terminal
workspaces have unknown current environment applicability. Reporting neither
reruns validation commands nor writes a temporary Git index. Historical success
does not establish present applicability or landing authorization.

Completion distributions separate task status and assessed outcome. The
`criteria_met_completion` population requires both `task_status=done` and
`outcome=met`, with eligible and total cohort counts. A quick canceled or
unassessed task cannot shorten that distribution. The separately named
`all_terminal_completion_elapsed` is descriptive of all terminal tasks.

Timing distinctions:

- Attempt elapsed time and completion elapsed time use runtime wall-clock
  timestamps. Completion spans the first attempt to the terminal attempt.
- Inter-attempt gaps use observed timestamps. They do not identify human wait,
  active work, or scheduler delay.
- Setup time measures the bounded handler call with a monotonic clock. Legacy
  handler action timings are preserved and never summed into a fabricated
  total.
- Validation time sums the actual measured commands within each invocation.
  Pending invocations remain explicitly missing.
- Landing, recovery, close and cleanup record the first completed mutating
  invocation for each durable phase. Exact no-op retries do not mutate history.
  Failed, partial, read-only, and unmeasured invocation counts remain unknown;
  these samples do not represent all command overhead or retry totals.
- Different phase measurements may overlap. They are not added together to
  infer total work time. Missing cost/token measurements are not inferred.

`successor_attempts` counts actual append-ordered successor attempts. It does
not estimate retries within an attempt. Reviewer assertions and criterion
corrections retain their provenance independently from execution and landing.

## Storage and compatibility

Runtime remains `blackdog.runtime/v4`. Existing tasks, attempts and event bytes
are neither relabeled nor migrated. New strict schema-1 `task.evidence.*`
records share `events.jsonl`; they do not create a second lifecycle store.
History without definitions, assessments or measurements remains explicitly
missing. Existing `{name,status}` validation rows remain caller declarations.

Input files are bounded to 64 KiB. Duplicate JSON keys, unknown fields or
versions, malformed identities, broken references and competing correction
chains fail closed. Event admission takes the runtime lock before the event
lock and validates actual task/attempt ownership. Evidence reports validate
known evidence before exposing outcome success. Evidence-write failure after
an already completed lifecycle mutation is reported as missing measurement,
while the original lifecycle result and exact recovery action are preserved.
