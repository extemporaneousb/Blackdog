# Historical Maintenance Review

This preserves the eleven-item review at
`9633328716595bb1c677a8c5b74dd9fc2df7069c` and its bounded closeout. The finding
IDs and original evidence remain historical references; this is no longer the
live product-work ledger. No P0 was established by that review.

Implemented fixes, measured decisions and explicit scope boundaries are recorded
below. Integrated local acceptance passed for the bounded closeout.
Remaining accepted preparation requirements belong only in
[Residual work](RESIDUAL_WORK.md). Neither document creates tasks, selects work,
authorizes execution, or replaces the canonical task runtime.

## Closeout Validation

`make acceptance` passed on macOS with Python 3.14.6: 221 tests in 181.383
seconds, public checks over 88 candidate files, a release build, and one passing
external portable-artifact lifecycle sample. The accepted archive SHA-256 is
`0dda7976e9f386733239262e3a3851e05acb9441a063ea730bfe5fc00af98386`,
the same artifact used for the final lifecycle comparison below.

The P1 changes passed 38 focused tests, four existing distribution containment
checks, and four source-CLI unbind smoke cases. Maintenance changes passed the
inherited-Git and hung-process regressions plus 62 product-surface, architecture,
evidence and lifecycle tests. An independent review of that subset found no
concrete P0/P1 blocker or introduced P2 regression; its additional continuous
stdout/stderr timeout probes and seven focused tests passed.

Preparation review corrections passed five named checks in 9.364 seconds and
independent archive-byte verification. A direct packaged Python-recipe begin
smoke passed on the same artifact. A local mixed-project fixture recorded one
cold-preparation observation of 4.295 seconds and one verified same-worktree
reuse observation of 1.199 seconds; these are descriptive fixture timings, not
a broader dependency-cache benchmark.

This establishes the bounded local implementation and acceptance described
below. It does not establish hosted CI, representative-project deployment, the
broader platform/dependency matrix or every accepted preparation stage. The four
remaining requirement groups stay open in [Residual work](RESIDUAL_WORK.md).

## Reproduced Defects

### BD-MAINT-001 — Unbind ancestor-symlink containment

- **Severity / disposition:** P1 / fixed. Original confidence: high, reproduced.
- **Historical evidence:** a configured relative launcher was admitted lexically.
  With `.VE` pointing to a sibling directory, confirmed unbind removed that
  directory's `bin/blackdog`.
- **Delivered:** [membership removal](../src/blackdog/repo_membership.py) traverses
  ancestors through directory descriptors without following links. Unsafe
  ancestors are preserved; a final symlink is unlinked without following its
  target. Planned entries are rechecked, managed removals precede control data,
  and the profile is removed last. User instructions, unrelated content and
  legacy history are retained under those ownership rules.
- **Validation:** [containment tests](../tests/test_unbind_containment.py) cover
  internal, external, dangling and final links, ancestor replacement during
  removal, changed ownership, and failure ordering. Existing
  [distribution containment tests](../tests/test_runtime_distribution.py) remain.
  Unbind remains a multi-file operation, not an atomic filesystem transaction.

### BD-MAINT-002 — Validation deadline with detached descendants

- **Severity / disposition:** P1 / fixed. Original confidence: high, reproduced.
- **Historical evidence:** a separate-session child retained an output pipe for
  three seconds; a 0.1-second validation timeout returned `passed` after about
  3.07 seconds because buffered stream closure blocked.
- **Delivered:** [validation execution](../src/blackdog/validation.py) drains
  unbuffered, nonblocking pipes within the command deadline. Incomplete output
  at the deadline returns `timed_out`, even after a successful leader exit.
  Output remains count-only; termination stays within the owned process group.
- **Validation:** [validation tests](../tests/test_validation.py) exercise stdout,
  stderr and both detached pipe holders, closed pipes with a running command,
  read failures, normal output counts, privacy and process-group termination.
  Synthetic detached children have bounded lifetimes and explicit cleanup.

## Portability and Validation Follow-up

### BD-MAINT-003 — Hermetic Git fixture configuration

- **Severity / disposition:** P2 / fixed. Original confidence: plausible
  portability risk.
- **Historical evidence:** core, evidence and lifecycle fixtures set Git identity
  but inherited signing and hooks configuration.
- **Delivered:** [test process support](../tests/process_support.py) configures
  signing off and an empty hooks directory within each synthetic repository.
  Production policy and user configuration remain unchanged.
- **Validation:** [fixture regressions](../tests/test_runtime_distribution.py)
  supply inherited signing through an unavailable program and a failing hook;
  all three fixtures initialize, a linked-worktree commit succeeds, and inherited
  configuration bytes remain unchanged. Existing lifecycle tests still run real
  Git commands against isolated repositories.

### BD-MAINT-004 — Bound CLI test subprocesses

- **Severity / disposition:** P2 / fixed. Original confidence: plausible
  suite-stall risk.
- **Historical evidence:** distribution CLI tests launched subprocesses without
  deadlines, unlike the external artifact harness.
- **Delivered:** [distribution test support](../tests/test_runtime_distribution.py)
  uses a declared 120-second default bound, file-backed output and bounded
  termination of its owned process group. Timeout diagnostics include the
  command, limit and available output; lifecycle assertions are retained.
- **Validation:** a synthetic hung command fails at its one-second deadline
  within four seconds, exposes both output streams, and leaves no owned child
  running. Assertion-failure cleanup is also bounded.

### BD-MAINT-005 — Explicit local artifact acceptance entrypoint

- **Severity / disposition:** P2 / fixed and locally accepted.
  Original confidence: verified invocation gap.
- **Historical evidence:** `make acceptance` previously aliased the test suite;
  external artifact acceptance ran separately and in four release jobs.
- **Delivered:** the [Makefile](../Makefile) runs the suite once, then builds and
  checks one external artifact sample. `make acceptance-artifact` provides that
  artifact gate after a separate suite run. The
  [acceptance runbook](RUNTIME_ACCEPTANCE.md) describes both commands; the four-job
  [release workflow](../.github/workflows/release.yml) is unchanged.
- **Validation:** command expansion shows one suite invocation and one external
  sample; an injected harness failure makes the gate fail. The actual
  `make acceptance` run passed 221 tests, public checks, the release build and
  one external portable-artifact lifecycle sample, as recorded above.

## Maintenance Investigations

### BD-MAINT-006 — Durable-schema documentation checks

- **Severity / disposition:** P3 / fixed. Original confidence: verified
  maintenance coupling.
- **Historical evidence:** task-intent documentation checks depended on exact
  paragraph and newline formatting.
- **Delivered:** [product-surface tests](../tests/test_product_surfaces.py)
  normalize whitespace and check the three durable-intent claims independently,
  while rejecting the obsolete objective field.
- **Validation:** editorial reflow passes; removing each required claim or adding
  obsolete-field variants fails. Both current documents pass. Contract wording
  changes still require review of the corresponding assertions.

### BD-MAINT-007 — Core import-boundary enforcement

- **Severity / disposition:** P3 / implemented. Original confidence: proposed
  coverage.
- **Historical evidence:** the unused scanner in test support had no caller;
  core's package direction lacked a comprehensive static import check.
- **Delivered:** [architecture tests](../tests/test_architecture_boundaries.py)
  inspect every core Python file with the standard-library AST and reject static
  imports from `blackdog` or `blackdog_cli`.
- **Validation:** direct, aliased, nested, conditional and `from` imports are
  rejected; supported core, relative and standard-library imports, comments and
  literals pass. Existing provider-boundary tests remain. This rule enforces
  static package direction; it is not a general dynamic dependency analyzer.

### BD-MAINT-008 — Command latency profiling

- **Severity / disposition:** P3 / measured; closed without a latency change.
  Original confidence: historical measured investigation.
- **Historical evidence:** the seven-sample
  [release measurements](RELEASE_ACCEPTANCE.md#final-workload-measurements)
  recorded slower read, recovery, close and cleanup phases. They do not establish
  current latency.
- **Current comparison:** the [external lifecycle harness](RUNTIME_ACCEPTANCE.md)
  ran the same fresh-install, close, cleanup/replay and landing workload three
  times per immutable artifact on macOS with Python 3.14.6. Samples alternated
  baseline/candidate, candidate/baseline, baseline/candidate. Baseline source was
  `f278b1373e62857ddeb4b6d3c5d205c059bfa468`, archive SHA-256
  `64d24f77b06da70160e65abf2ddc7865871b48eb3c871bdadc501f9ae3abec65`.
  The corrected integrated candidate archive SHA-256 was
  `0dda7976e9f386733239262e3a3851e05acb9441a063ea730bfe5fc00af98386`.
  All six lifecycle scenarios passed.

  | Phase | Baseline median / p95, ms | Candidate median / p95, ms | Median change |
  |---|---:|---:|---:|
  | `begin_close` | 743.065 / 898.723 | 811.459 / 847.784 | +9.20% |
  | `begin_land` | 547.428 / 548.674 | 628.884 / 629.177 | +14.88% |
  | `cleanup` | 894.245 / 898.376 | 905.686 / 909.111 | +1.28% |
  | `cleanup_replay` | 636.241 / 645.363 | 655.156 / 658.340 | +2.97% |
  | `close` | 570.148 / 573.924 | 574.253 / 579.373 | +0.72% |
  | `install` | 278.498 / 286.346 | 289.897 / 292.488 | +4.09% |
  | `land` | 3924.602 / 4056.339 | 3930.371 / 3937.712 | +0.15% |
  | `recover` | 757.371 / 769.328 | 767.406 / 778.558 | +1.32% |
  | `recover_after_land` | 656.755 / 664.520 | 670.099 / 692.082 | +2.03% |
  | `scenario_wall` | 12969.417 / 13131.429 | 13191.316 / 13271.969 | +1.71% |
  | `show` | 763.435 / 782.920 | 772.431 / 789.332 | +1.18% |
  | `summary` | 167.651 / 172.397 | 181.784 / 183.469 | +8.43% |
  | `validation` | 24.787 / 25.021 | 24.895 / 25.097 | +0.44% |

- **Counts and limits:** every phase in each artifact had eligible/observed/missing
  counts of 3/3/0. p95 uses nearest rank and is the maximum of these three
  observations. These are descriptive synthetic local measurements, not stable
  tail estimates or a production-workload profile; host load and filesystem
  caches were uncontrolled. Scenario wall time includes fixture construction and
  unmeasured verification, so phases do not sum to that total. Both artifacts
  used the same content-validation assertion before landing. The candidate was
  frozen from integrated working source, not an already committed release.
- **Decision:** no latency optimization is justified by this bounded comparison.
  Median scenario wall time increased 1.71%; begin-close and begin-land increased
  68.394ms and 81.456ms respectively, while the measured show, recovery, close and
  cleanup changes ranged from 0.72% to 2.97%. Summary increased 14.133ms (8.43%).
  These observations identify the costs without establishing a repeated expensive
  operation, a production bottleneck or a safe optimization target. No speculative
  caching or lifecycle rewrite was added.

### BD-MAINT-009 — Repeated test artifact builds

- **Severity / disposition:** P3 / measured; closed without a cache.
  Original confidence: speculative optimization.
- **Historical evidence:** distribution tests repeatedly built release bytes;
  the share of test-module runtime was unknown.
- **Measurement:** three serial runs of the
  [distribution tests](../tests/test_runtime_distribution.py) passed all 19 tests
  each. The measured integrated candidate archive SHA-256 was
  `059c019661027cc05f78df495ce31a3ff372e2a4f39e8051bd760d0dd459497b`;
  source identities matched that snapshot before and after each run. This
  snapshot preceded the final preparation review corrections, which did not
  change release construction or the cache decision.

  | Run | Release builds | Build time, ms | Module wall time, ms | Build share |
  |---|---:|---:|---:|---:|
  | 1 | 17 | 261.307 | 18,831.823 | 1.388% |
  | 2 | 17 | 267.209 | 18,455.159 | 1.448% |
  | 3 | 17 | 259.588 | 18,295.446 | 1.419% |

- **Decision:** no cache. The median build share was 1.419%; even eliminating all
  measured construction would save little module time. Safe consumer-only reuse
  would save less because reproducibility, changed-source, manifest,
  update-digest and corrupt-artifact cases still need independent builds.
  Mutable fixture repositories remain isolated.
- **Limits:** eligible/observed/missing counts were 3/3/0 for run-level values
  and 51/51/0 for build invocations. These are descriptive samples on macOS with
  Python 3.14.6. Instrumentation times parent-process `release_bytes` calls;
  subprocess CLI costs remain in the wall-time denominator. This is not a
  full-suite percentage or stable tail estimate; host load and filesystem caches
  were uncontrolled. Final suite execution covers distribution correctness
  again without repeating this profiling exercise.

### BD-MAINT-010 — Worktree preparation design delivery

- **Severity / disposition:** accepted product design / bounded first slice
  implemented and locally accepted; remaining criteria transferred.
- **Historical evidence:** the accepted
  [preparation contract](WORKTREE_PREPARATION.md) distinguished runtime isolation
  from broader repository preparation. Its P1-P5 labels name implementation
  stages, not finding severities.
- **Delivered scope:** a reviewed recipe supports task-owned cold setup and
  verified reuse within the same worktree, with fresh readiness checks. It binds
  declared tools, tracked inputs, explicitly admitted input files and owned
  outputs. The canonical attempt is claimed before recipe effects; private
  intent and completion evidence support retry verification and block
  indeterminate interrupted effects. See the
  [implementation](../src/blackdog/preparation.py) and
  [preparation tests](../tests/test_preparation.py).
- **Local validation:** the integrated 221-test suite, direct packaged Python
  recipe begin, external portable-artifact lifecycle and independent correction
  review passed on macOS with Python 3.14.6. The mixed-project fixture's single
  cold/reuse observations were 4.295/1.199 seconds. These prove the exercised
  local cases, not a general performance claim or the broader matrix.
- **Disposition boundary:** this closes the historical item by implementing that
  portion and transferring actual undelivered requirements to
  [Residual work](RESIDUAL_WORK.md). It does **not** declare all P1-P5 acceptance
  complete. Shared immutable-cache qualification, recipe discovery/proposals,
  broader dependency/platform proof and representative rollout/measurement
  remain explicit there. Hosted CI and real-project rollout remain outside this local proof.

### BD-MAINT-011 — Remaining terminology and reporting structure

- **Severity / disposition:** P3 / help corrected; reporting rewrite declined.
  Original confidence: maintenance suggestions, not reproduced defects.
- **Historical evidence:** public CLI descriptions exposed `WTAM` terminology,
  while the outcome-report projection was large.
- **Delivered:** four [CLI help](../src/blackdog_cli/main.py) descriptions use task,
  worktree or workspace language. Help smoke and parser inventory checks retain
  command names, arguments, destinations, choices and behavior.
- **No rewrite:** [outcome reporting](../src/blackdog/outcome_reporting.py)
  explicitly assembles schema, provenance, applicability and missingness using
  existing helpers. Inspection established neither a duplicated algorithm nor a
  measured bottleneck. Broad extraction would add schema/order review risk
  without an evidenced benefit, so no reporting refactor is carried forward.
