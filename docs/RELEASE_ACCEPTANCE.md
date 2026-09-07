# Environment Independence and Outcome Acceptance

The delivered core is **task, attempt, and evidence**. A task owns intent, an
attempt owns execution and integration, and typed evidence records observed
validation or caller-recorded outcome assessments. Repository policy remains in
`blackdog.toml`; structured `next_action` remains execution authority.

This report records local acceptance of the combined implementation. Final
canonical landing, terminal A1-A9 assessments, and exact-commit CI results are
separate delivery evidence. The acceptance task records these through the
shipped outcome protocol; machine-local receipts are not public source fixtures.

## Delivered work

| Item | Canonical main commit | Evidence |
| --- | --- | --- |
| Core decision and A1-A9 plan | `6b129a6912c480e8f94e43537338a93068348dcd` | [Accepted decision](EXECUTION_OUTCOMES_PLAN.md). |
| Isolated lifecycle acceptance harness | `32ebe9a5ece4099b29d8140d40eb24d167673eec` | [Harness contract](RUNTIME_ACCEPTANCE.md), four focused harness tests. |
| Portable runtime and release workflow | `c4adda442459ccf8f07ed8919690894052190ceb` | [Distribution contract](RUNTIME_DISTRIBUTION.md), independent containment and recovery review. |
| Typed outcomes, validation, and measurements | `d6e109f8d133782eace25937fc7a16f145dcfd22` | [Evidence contract](OUTCOME_EVIDENCE.md), strict schema and fault tests, independent artifact acceptance. |

The final code tree corresponds to reviewed source `9b1bceda5eb9246c20b1b209c0d749135e5aa03f`.
Its reproducible archive SHA-256 is
`80aa190a11e322fab0a5357955bd9feaa1985969a5b7c4c0f704928872b0f74c`.
Documentation and benchmark reports do not change archive contents.

The integrated suite passed **164 tests** in 143.690 seconds at `a99455d`.
The final source delta changes CLI help and documentation only; eight product
surface tests and public-check passed afterward. Independent review also ran
all 31 evidence tests and the exact final archive through definition admission,
machine validation, assessment, canonical landing, cleanup, terminal reporting,
and completed validation replay. The release hash recorded by the validator
matched the executed archive. No provider data was read.

The real delivery task adopts the same protocol: an immutable A1-A9 definition,
a stable product-validation run identity, and explicitly reviewer-asserted agent
assessments. The final criterion requires clean canonical `main`, terminal
proof, and all four exact-commit release CI jobs with matching archive hashes.
Naming the reviewing agent is an assertion, not authenticated human acceptance.
A validation replay after cleanup preserves the original receipt and reports
unknown current environment applicability; a terminal tree-bound assessment
remains a separate fact.

## Final workload measurements

[Baseline samples](evidence/runtime-baseline-6059918.json) use an isolated,
unchanged checkout of `605991856e54cbf34804365e241c4455ca6c09ec`.
[Final samples](evidence/runtime-final-80aa190.json) use the combined archive
identified above. Each side has seven independent fresh-repository scenarios,
seven successful canonical landings, fourteen terminal tasks, and no missing
phase samples. Each scenario checks install, begin, show, recovery, close,
cleanup, exact post-cleanup replay, fixture-content validation, landing, and
terminal read surfaces. Candidate root and task workspaces have no `.VE`.

Both runs use macOS and the same pinned CPython 3.14.6 interpreter. Each command
starts a new process. The baseline retains its explicit environment handlers;
the artifact requires no source import path, site packages, or project Python
environment. The original per-fixture archive is deleted after installation to
exercise the installed immutable runtime. This is process/import isolation,
not a filesystem sandbox.

The frozen baseline ran first. The final combined candidate ran later, after
evidence integration; an intermediate runtime-only measurement is not used as
the final result. Other worker test loads were paused for the measurement
windows. Execution order was not randomized, and ambient operating-system load
and filesystem caches were not controlled. These are descriptive local results,
not a significance or production-latency claim.

All durations below are milliseconds. Change is relative to baseline median;
a negative value is faster. P95 uses nearest rank and is the maximum at n=7.
It does not estimate a stable tail latency.

| Phase | Baseline median | Final median | Change | Baseline p95 | Final p95 |
| --- | ---: | ---: | ---: | ---: | ---: |
| Install | 3975.1 | 279.6 | -93.0% | 4045.5 | 285.9 |
| Begin before close | 2406.6 | 740.9 | -69.2% | 2509.6 | 820.9 |
| Begin before land | 2137.1 | 557.4 | -73.9% | 2175.7 | 563.7 |
| Show | 619.1 | 784.4 | +26.7% | 625.0 | 801.0 |
| Recover | 615.5 | 790.0 | +28.4% | 639.8 | 876.0 |
| Close | 401.9 | 577.6 | +43.7% | 410.0 | 581.4 |
| Cleanup | 810.0 | 935.0 | +15.4% | 826.2 | 940.0 |
| Exact cleanup replay | 489.0 | 659.3 | +34.8% | 497.7 | 663.6 |
| Fixture validation | 25.2 | 24.8 | -1.3% | 26.0 | 25.7 |
| Land | 3958.6 | 4134.1 | +4.4% | 4059.3 | 4252.8 |
| Recover after land | 505.0 | 674.6 | +33.6% | 517.2 | 680.0 |
| Summary | 163.0 | 172.0 | +5.6% | 203.5 | 177.6 |
| Whole scenario | 18405.8 | 13290.8 | -27.8% | 18653.0 | 13483.5 |

The whole scenario is **27.8% faster**, with installation **93.0% faster** and
begin-before-land **73.9% faster**. Removing environment creation addresses the
measured setup cost; command-local imports reduce avoidable startup work.
Several individual commands remain slower in the source-based Python archive,
including close, recovery, and cleanup replay. The table retains every measured
regression. Whole-scenario time includes fixture setup and unlisted read checks;
phase durations overlap and are not a decomposition of that total.

This workload does not measure model execution, human waiting, network services,
or large-history scaling. Fixture validation is identical on both sides; the
new product validation protocol is exercised separately by typed acceptance.
The archive still requires Python 3.11 or newer and Git. Explicit project Python
handlers remain available, and existing environments are preserved.

## Review and recovery proof

Independent review returned concrete failures to the owning workers before
acceptance. Corrected cases include source symlink/traversal/nonregular-file
reads, first-install and update writes through control-directory symlinks,
profile-free recovery digest checks, and unhandled worktree errors after lazy
imports. Synthetic containment cases verified rejection with no outside writes.

Evidence review verified task-worktree registration and start ancestry before
validator effects, task-local observation locks that allow unrelated work to
continue, strict native JSON types, correction compare-and-swap, explicit
unknown runtime identity, and current versus historical validation applicability.
Measurement-write failure preserves the completed lifecycle result and its exact
recovery action. Regression reports count distinct criteria separately from
repeated correction transitions; compact reports preserve provenance and missing
intervention/assessment coverage.

Fault checks cover unavailable invocation intent, interruption before a durable
result, response loss after durable result, and concurrent identical run IDs.
Only one concurrent invocation executes; a durable receipt is replayed without
reexecution, while an incomplete invocation remains indeterminate. Existing
migration and landing fault tests remain in the integrated suite.

An independent synthetic compatibility check used the original `6059918`
reader and writer against a v4 task with new setup measurement and typed outcome
events. It preserved exact runtime bytes and the complete event prefix while
appending a legacy event; the current reader still derived the same assessed
outcome. No live store schema migration was performed.

Supported primary `repo update` and `repo refresh` selected the combined snapshot
without tracked changes and preserved the previous runtime snapshot. Actual
post-land task cleanup and recovery are verified through emitted commands and
terminal evidence, without deleting unrelated worktrees.

## CI and limits

The [release workflow](../.github/workflows/release.yml) defines Linux/macOS
and Python 3.11/3.14 jobs, full tests, reproducible builds, external lifecycle
acceptance, and artifact/checksum uploads. Workflow configuration and local
checks do not establish a successful remote run. The final delivery reports
actual run status for the exact pushed commit and independently checks downloaded
archive hashes against their checksums, acceptance reports, and the local archive.
The external delivery proof pairs CI run identity and artifact checks with the
native outcome report; native assessment references remain validation event IDs.

Outcomes remain caller-recorded assessments; reviewer identity is unauthenticated.
Validation observes Git content, configured commands, and a bounded runtime
descriptor. Ambient environment values, arbitrary untracked importable runtime
code, external dependencies, and services are unobserved. No cache, new landing
policy, scheduler, provider transcript store, or second mutable task store is
introduced. Missing costs, waits, interventions, and invocation counts remain
missing rather than being inferred from unrelated timing or retry evidence.
