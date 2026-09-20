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

The baseline provides a reviewed repository recipe with task-owned cold setup
and verified reuse of completed outputs in the same worktree. PREP-001 extends
it with optional qualified shared immutable file bundles; task environments
remain independently owned and every path runs fresh readiness checks.

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

Those baseline observations prove the exercised local fixtures. PREP-001's
additional bounded coverage is recorded below. Representative-project rollout
and the broader dependency/platform guarantees remain separate.

## PREP-001 — Qualify Shared Immutable Dependency Reuse

**State:** implemented for repository-local immutable regular-file bundles on
trusted POSIX filesystems. This qualifies the bounded shared-reuse mechanism in
P3/P4; it does not close all dependency/platform or rollout proof in those stages.

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

**Delivered:** opt-in `shared_artifact` recipe policy with required compatibility
probes, isolated declared-input builds, read-only content inventories, per-key
publication locks, atomic verified generation/pointer publication, and fresh
task-owned copies. Identity binds the complete recipe/config, declared tracked
and admitted ignored inputs, executable/version identities, ABI probes and
platform. Changed keys build anew; corrupt generations rebuild without altering
old generations or active-task dependencies. Task setup and readiness retain
their existing fail-closed ownership and interruption rules.

**Acceptance coverage:** `tests/test_preparation_cache.py` exercises concurrent
consumers, bounded lock waits, identity mismatch, corruption, malformed/missing
publication state, link/special-file rejection, materialization drift, and
interruptions during build, generation rename and before/after pointer
publication. `tests/test_preparation.py` exercises canonical blocked-attempt
preservation, new-task recovery, unchanged task receipts, ABI drift, failed or
mutating bundle checks, and equivalent cold/shared/mismatch application readiness.
The representative offline Python workload installs a hash-locked wheel into a
fresh virtual environment, creates an editable task package, verifies imports
and console entrypoints after distinct task-source edits, and confirms that
primary and earlier active environments remain usable after rebuild.

Each workload run emits input/artifact/wheel digests and one elapsed observation
per cold/shared/mismatch path. These small local fixtures establish behavior,
not a production rollout or speedup claim. The bundle is a vendored pure-Python
wheel; no download is measured. The full supported OS/interpreter matrix for
this addition must be established by hosted runs, not inferred from local tests.

**Unsupported/residual:** shared mutable environments, copying virtual
environments, symlink/hardlink bundles, remote caches/network filesystems,
automatic cache eviction or orphan promotion, automatic discovery of tool or
native ABI dependencies, and reproducibility of unpinned arbitrary recipes.
Candidates and superseded generations are retained for inspection, so storage
can grow. Read-only modes do not defend against a hostile local user or trusted
recipe deliberately writing outside its declared workspace. Broader native and
Node dependency proof remains PREP-003; real-project adoption and repeated
equivalent-workload measurement remain PREP-004. PREP-002 is unchanged.

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
