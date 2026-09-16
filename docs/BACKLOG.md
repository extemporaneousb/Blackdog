# Maintenance Backlog

This is the human follow-up ledger for the bounded maintenance review at
`9633328716595bb1c677a8c5b74dd9fc2df7069c`. It does not create tasks, select work,
authorize execution, or replace the canonical task runtime. Each implementation
requires its own scope and normal task workflow.

Keep these IDs stable and retain unresolved entries. Update state and evidence
when work is accepted; resolved entries must link the change and its validation.
Severity describes the finding, while confidence distinguishes reproduced
defects, plausible risks, measured investigations, and proposed design work.
All entries below are deferred; no P0 was established by this bounded review.

## Reproduced Defects

### BD-MAINT-001 — Unbind ancestor-symlink containment

- **Severity / state:** P1 / deferred. **Confidence:** high, reproduced.
- **Evidence:** [Repository membership removal](../src/blackdog/repo_membership.py)
  admits a relative legacy launcher lexically and unlinks it without proving
  ancestor containment. In a synthetic repository, `.VE` pointed to a sibling
  directory; confirmed unbind removed that directory's `bin/blackdog`.
- **Scope:** legacy-launcher preview and removal ownership checks.
- **Close when:** preview and confirmed unbind safely handle internal, external,
  dangling, and final symlinks. Escaping ancestors cannot authorize external
  deletion, while removal of a final symlink never follows its target. Retain
  existing [distribution containment tests](../tests/test_runtime_distribution.py).

### BD-MAINT-002 — Validation deadline with detached descendants

- **Severity / state:** P1 / deferred. **Confidence:** high, reproduced.
- **Evidence:** [Validation output draining](../src/blackdog/validation.py) can
  block on buffered stream closure when a detached descendant retains a pipe.
  A synthetic command exited after starting a separate-session child that slept
  for three seconds; a 0.1-second timeout returned `passed` after about 3.07 seconds.
- **Scope:** end-to-end validation deadlines and incomplete output drains.
- **Close when:** a detached child retaining stdout or stderr cannot extend the
  deadline or produce a successful incomplete-drain result. Preserve existing
  timeout and process-group guarantees in the
  [validation tests](../tests/test_validation.py); synthetic children must have
  deterministic cleanup even when an assertion fails.

## Portability and Validation Follow-up

### BD-MAINT-003 — Hermetic Git fixture configuration

- **Severity / state:** P2 / deferred. **Confidence:** plausible portability risk.
- **Evidence:** [Core fixtures](../tests/core_audit_support.py),
  [evidence fixtures](../tests/test_evidence.py), and
  [lifecycle fixtures](../tests/test_wtam_lifecycle.py) set identities but inherit
  signing and hooks configuration. The
  [artifact acceptance harness](../scripts/acceptance_runtime.py) disables signing
  and uses an empty hooks path explicitly.
- **Scope:** minimal fixture configuration; retain real Git integration coverage.
- **Close when:** fixtures pass with synthetic inherited signing and hooks
  settings, without changing production Git policy or sharing mutable repositories.

### BD-MAINT-004 — Bound CLI test subprocesses

- **Severity / state:** P2 / deferred. **Confidence:** plausible suite-stall risk.
- **Evidence:** [Distribution CLI tests](../tests/test_runtime_distribution.py)
  invoke subprocesses without timeouts; the
  [artifact harness](../scripts/acceptance_runtime.py) already bounds its commands.
- **Scope:** test subprocess deadlines and useful failure context.
- **Close when:** a synthetic hung command fails within a declared bound, reports
  the command and available output, and leaves no child process behind. Preserve
  all existing lifecycle assertions.

### BD-MAINT-005 — Explicit local artifact acceptance entrypoint

- **Severity / state:** P2 / deferred. **Confidence:** verified invocation gap.
- **Evidence:** [Makefile](../Makefile) aliases `acceptance` to `test`. The external
  lifecycle harness runs separately and in all four jobs of the existing
  [release workflow](../.github/workflows/release.yml), as clarified in the
  [acceptance runbook](RUNTIME_ACCEPTANCE.md).
- **Scope:** decide whether to add a local convenience gate that includes a
  single external artifact sample; repeated timing measurements remain separate.
- **Close when:** the chosen local entrypoint and documentation agree about
  exactly which checks run. Any added gate must fail on harness failure and
  preserve the existing four-job hosted acceptance coverage.

## Maintenance Investigations

### BD-MAINT-006 — Durable-schema documentation checks

- **Severity / state:** P3 / deferred. **Confidence:** verified maintenance coupling.
- **Evidence:** [Product-surface tests](../tests/test_product_surfaces.py) retain
  exact paragraph and newline assertions for the task-intent schema.
- **Scope:** evaluate semantic documentation checks if wording maintenance recurs.
- **Close when:** any replacement preserves meaningful positive schema claims
  and rejection of obsolete fields while allowing harmless editorial reflow.

### BD-MAINT-007 — Core import-boundary enforcement

- **Severity / state:** P3 / deferred. **Confidence:** proposed coverage.
- **Evidence:** the unused scanner in [core test support](../tests/core_audit_support.py)
  had no caller and was removed. The package boundaries in
  [Architecture](ARCHITECTURE.md#layers) currently have no comprehensive automated
  import-boundary rule.
- **Scope:** decide whether a small, explicit architecture rule is warranted.
- **Close when:** either document why no rule is needed, or introduce a tested
  rule that detects representative prohibited imports and accepts supported
  imports without replacing existing provider-boundary tests.

### BD-MAINT-008 — Command latency profiling

- **Severity / state:** P3 / deferred. **Confidence:** historical measured investigation.
- **Evidence:** [Release acceptance measurements](RELEASE_ACCEPTANCE.md#final-workload-measurements)
  record slower read, recovery, close, and cleanup phases in the historical
  seven-sample comparison. Those results do not establish current latency.
- **Scope:** fresh equivalent-workload profiling before selecting optimizations.
- **Close when:** comparable current measurements identify actionable costs or
  explain why no change is justified, with per-command results, denominators,
  missingness, and small-sample limits preserved.

### BD-MAINT-009 — Repeated test artifact builds

- **Severity / state:** P3 / deferred. **Confidence:** speculative optimization.
- **Evidence:** [Distribution tests](../tests/test_runtime_distribution.py)
  repeatedly build release bytes; their share of suite time has not been measured.
- **Scope:** profile before considering immutable artifact reuse in consumer tests.
- **Close when:** measurements justify a limited cache or show no benefit.
  Reproducibility, changed-source, manifest, update-digest, and corrupt-artifact
  cases must remain independently built; mutable repositories must stay isolated.

### BD-MAINT-010 — Worktree preparation design delivery

- **Severity / state:** unassigned product design / deferred.
  **Confidence:** accepted design with explicit implementation gaps.
- **Evidence:** [Worktree preparation](WORKTREE_PREPARATION.md#current-coverage-and-gaps)
  distinguishes shipped runtime isolation from future repository preparation.
- **Scope:** separately deliver the existing
  [staged implementation and acceptance](WORKTREE_PREPARATION.md#staged-implementation-and-acceptance).
  Its P1-P5 labels are implementation stages, not finding severities; these
  stages remain unimplemented by this maintenance pass.
- **Close when:** the referenced design's delivery and acceptance criteria have
  their own reviewed implementation and evidence. Keep the detailed design there.

### BD-MAINT-011 — Remaining terminology and reporting structure

- **Severity / state:** P3 / deferred. **Confidence:** maintenance suggestions.
- **Evidence:** [CLI help](../src/blackdog_cli/main.py) still uses internal `WTAM`
  terminology in several descriptions, and
  [outcome reporting](../src/blackdog/outcome_reporting.py) contains a large
  projection function. Neither observation establishes a correctness defect.
- **Scope:** evaluate specific clarity or decomposition changes when they have
  a demonstrated maintenance benefit; avoid a blanket rewrite.
- **Close when:** selected changes have evidence of improved clarity and preserve
  parser behavior, report schemas, ordering, provenance, and missingness, or the
  proposal is declined with a recorded rationale.
