# Residual Work

This is the single live ledger for accepted preparation requirements that remain
undelivered or lack the required proof after the bounded first slice. The full
architecture and acceptance criteria remain in the
[worktree preparation contract](WORKTREE_PREPARATION.md); the eleven-item
[maintenance review](BACKLOG.md) is historical.

These entries describe remaining scope, not automatic authorization to begin
new work. Each delivery needs an explicit scope, normal task workflow, review
and evidence. They do not create a second task store or continuation authority.

## Implemented Baseline

The current slice provides a reviewed repository recipe with task-owned cold
setup and verified reuse of completed outputs in the same worktree. Reuse runs
fresh readiness checks; it is not a shared dependency cache.

Declared tools and tracked inputs are checked, explicitly admitted ignored
input files are verified before materialization, and output ownership and
identities are checked. The canonical attempt is claimed before recipe effects.
Private intent and completion evidence bind preparation to that attempt and
source; interrupted effects remain indeterminate and blocked instead of being
silently replayed. The [implementation](../src/blackdog/preparation.py) and
[tests](../tests/test_preparation.py) are the current behavioral references.

Integrated local acceptance passed on macOS with Python 3.14.6: 221 tests,
public checks, release construction and one external portable-artifact lifecycle
sample. The accepted archive SHA-256 is
`0dda7976e9f386733239262e3a3851e05acb9441a063ea730bfe5fc00af98386`.
A direct packaged Python-recipe begin smoke and independent review of the
preparation corrections also passed. The local mixed-project fixture recorded
one cold-preparation observation of 4.295 seconds and one verified same-worktree
reuse observation of 1.199 seconds. These timings are descriptive, not a shared
cache benchmark. [Closeout validation](BACKLOG.md#closeout-validation) records
the bounded evidence.

This proves the exercised local fixtures; hosted CI, representative-project
rollout and the broader dependency/platform guarantees remain separate. None
of the four accepted requirement groups below is closed by local synthetic
acceptance.

## PREP-001 — Qualify Shared Immutable Dependency Reuse

**State:** open; the current reuse path is confined to verified outputs of the
same worktree. Covers the remaining shared-reuse portions of stages P3 and P4.

**Acceptance:**

- Bind reusable artifacts to the recipe, relevant configuration, manifests,
  lockfiles, declared inputs, runtime/compiler/package-manager identities, ABI
  and platform. Changed identities must invalidate affected reuse.
- Publish only verified immutable artifacts. Concurrent tasks must never consume
  partial artifacts or alter another running task's dependencies; rebuilding or
  replacing a candidate must leave primary and active-task environments intact.
- Demonstrate equivalent application readiness for cold preparation and shared
  reuse, including task-checkout imports and entrypoints, mismatch/rebuild,
  interrupted publication and recovery. Report the artifact and input identities
  that make each successful reuse applicable.

## PREP-002 — Review Recipe Discovery Proposals

**State:** open; current execution requires an explicitly authored, reviewed
recipe. Covers the undelivered discovery/proposal portion of stage P2.

**Acceptance:**

- Inspect tracked manifests to propose a persistent recipe with declared inputs,
  tools, effects and readiness checks; classify known, unknown and unsupported
  requirements and explain the proposed check coverage.
- Require review to make the versioned proposal authoritative. Discovery must
  not execute inferred setup during each begin, scan private files to guess
  requirements, or silently replace existing handler policy.
- Prove missing, changed and incompatible declarations cannot produce ready,
  while reviewed policy upgrades preserve existing environment/history ownership.

## PREP-003 — Broaden Dependency, Native Output and Platform Proof

**State:** open proof coverage under stages P1-P4. The current Python local-wheel
and editable-package fixture and dependency-free locked Node/mixed build fixture
exercise useful checkout mechanics; they do not establish all dependency and
platform guarantees in the accepted contract.

**Acceptance:**

- Exercise representative locked Node package dependencies, Python dependencies
  with native outputs, and mixed generated/native outputs. Imports, console
  entrypoints, builds and tests must use the intended task checkout and declared
  dependencies under both cold and qualified reuse paths.
- Verify explicitly declared external availability requirements, including
  unavailable inputs/services, without recording secret contents or publishing
  readiness for requirements that were not checked.
- Record results across the supported Linux/macOS and interpreter/toolchain
  matrix, including relevant runtime-isolation and version-change checks.
  Exercise paths with spaces, post-readiness drift, failed dependency acquisition
  and recovery after workspace removal. Report unsupported or untested cases
  explicitly; do not infer them from one local fixture run.

## PREP-004 — Representative Rollout and Equivalent-Workload Measurements

**State:** open; stage P5 has not been established by synthetic lifecycle or
single-sample fixture timing.

**Acceptance:**

- Adopt reviewed recipes in representative projects with a recoverable prior
  configuration and runtime. Verify the same application readiness requirements
  before comparing preparation paths.
- Measure cold, warm, mismatch/rebuild and recovery paths. Identify exact source
  and recipe revisions, dependencies, interpreter/toolchain, platform, cache
  state and readiness assertions; distinguish downloads from local preparation.
- Report sample counts, eligible/observed/missing and failed samples, per-phase
  distributions, regressions and small-sample limits. A warm-cache speedup closes
  no criterion unless its readiness proof matches the cold build.
