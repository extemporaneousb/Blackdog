# Runtime and worktree preparation contract

This is the accepted architecture contract. The runtime isolation boundary is
implemented under the invocation conditions below. The repository preparation
requirements are a target for subsequent implementation, not a claim that the
current handlers satisfy them. This document adds no shipped command, flag,
configuration field, or durable schema.

## One entrypoint, two responsibilities

`task begin` remains the one normal entrypoint. It must both run Blackdog from
an identifiable runtime and prepare the task checkout according to repository
policy. These are independent responsibilities within the same workflow; users
do not choose separate preparation modes. Under the target contract, begin
returns a workspace ready for declared commands under recorded inputs, or an
exact blocked/recoverable `next_action`. Readiness establishes the ability to
work; it does not establish that the task's eventual outcome is correct.

### Blackdog runtime

Installed consumers execute an immutable, digest-addressed Blackdog archive.
Supported isolated invocation is direct execution of that archive or the
installed launcher, or explicit `python3 -I -S /path/to/blackdog.pyz`. The direct
executable's shebang selects `python3` through `PATH` and supplies `-I -S`.
Python's [isolated mode](https://docs.python.org/3/using/cmdline.html#cmdoption-I)
ignores `PYTHON*` settings and unsafe/user import paths;
[`-S`](https://docs.python.org/3/using/cmdline.html#cmdoption-S) disables automatic
`site` initialization and its package-path and `.pth` processing. Blackdog's
own modules come from the selected archive, without third-party packages or a
project `.VE` dependency. Installing or upgrading unrelated Python packages
must not change those imports under this invocation contract.

The interpreter selected by `PATH`, its standard library and native libraries,
Git, and the operating system remain trusted dependencies. Activating an
environment can select a different interpreter. Python upgrades or replacement
of a selected executable can change behavior; the archive does not pin or fully
hash the interpreter. The current supported floor is Python 3.11 on Linux or
macOS. Compatibility must be demonstrated on the supported release matrix,
including rejection of unsupported Python versions. This is import isolation,
not a security sandbox or an assurance against arbitrary machine changes.

An explicit `python3 /path/to/blackdog.pyz` bypasses the shebang flags and does
not currently enforce isolation itself. That invocation must not be presented
as equivalent. A future entrypoint flag check could reject this invocation
before importing Blackdog, but cannot undo Python startup hooks already run.
Legacy `.VE` or `PYTHONPATH` launchers do not inherit the standalone assurance;
rollout must replace those launchers with the supported invocation consistently.
Blackdog development deliberately executes checkout source via
`scripts/blackdog`, using isolated Python and the script's own source path;
recovery retains its immutable archive outside the disposable worktree. See
[runtime distribution](RUNTIME_DISTRIBUTION.md) for installation and updates.

### Repository preparation

The repository owns a versioned, reviewed preparation recipe through
`blackdog.toml` and its repository policy. Blackdog owns deterministic
execution, ownership, verification, and receipts. A recipe specifies bounded inputs, tools, effects,
and readiness checks; it is not another task planner or a generated skill's
private setup algorithm. The following are semantic requirements, not proposed
configuration keys:

1. **Explicit policy.** Discovery may examine tracked manifests and propose a
   persistent recipe, explaining unsupported or unknown requirements. Review
   makes that recipe authoritative. Discovery cannot establish that every
   implicit requirement was found: classify known, unknown and unsupported
   requirements and state readiness-check coverage. Begin executes that
   revision; it does not infer and run a new setup strategy on every attempt. Missing or
   incompatible declarations produce a bounded blocker.
2. **Exact source and inputs.** Preparation binds the repository, task, attempt,
   recorded Git base/tree, recipe revision, and observed toolchain. Resolve the
   recipe and manifests consistently from the selected task base/checkout, not
   a moving primary checkout. Observe an input snapshot and verify it still
   matches before publishing readiness; changed inputs during preparation must
   discard the candidate or block, never produce readiness for mixed inputs.
   Only declared ignored or untracked inputs may be materialized. Each needs an explicit
   source, destination, identity or verification rule, permissions, and cleanup
   ownership. Do not copy a primary checkout wholesale or scan private files to
   guess requirements. External services, local configuration and secrets need
   explicit availability checks; receipts must not contain secret contents.
3. **Qualified reuse.** Reuse immutable dependency artifacts, or verify and
   rematerialize dependencies into an owned worktree environment. Qualification
   binds recipe and relevant configuration, manifests, lockfiles, declared
   inputs, runtime/compiler/package-manager versions, ABI and platform. A cache
   path, a matching Python version, or an existing `.VE` alone proves nothing.
   Dependency reuse identity may exclude source unrelated to dependencies, but
   the attempt's preparation receipt still binds its actual source. An unpinned
   dependency resolution cannot be reported as reproducible.
4. **Correct checkout execution.** Imports, editable packages, generated files,
   console scripts, native extensions and test tools must resolve to the intended
   task checkout and qualified dependencies. Do not copy virtual environments or
   accept a primary-environment script whose shebang silently runs primary
   source. Shared artifacts must remain immutable for their lifetime of use;
   verification before attaching a live mutable package directory is insufficient.
   Reuse must not let another task alter a running task's dependencies.
5. **Change and failure behavior.** A changed lockfile, recipe, toolchain,
   declared input or dependency artifact invalidates affected readiness and
   reuse. Rebuild in owned temporary state and publish only after verification,
   or block with the exact missing requirement. Do not mutate a primary
   environment, another task, or a shared artifact in place. Preserve unrelated
   changes. Source changes during work require relevant readiness checks again;
   preparation success does not remain applicable merely because it was once
   recorded. Unrelated source edits need not rebuild dependencies whose declared
   setup inputs are unchanged; readiness still retains exact source applicability.
6. **Typed evidence and recovery.** A receipt records identities, declared input
   coverage, observed toolchain/dependency identities, phases, verification
   results and readiness for the attempt. Unknown or unverified requirements
   remain explicit. Intent precedes effects; completed retries verify retained
   evidence before reuse. An interrupted install is resumable only under a
   defined idempotent or recoverable step contract. Unknown effects remain
   indeterminate and blocked; append-once evidence cannot make arbitrary
   commands execute exactly once. Structured lifecycle `next_action` remains
   the sole authority for continuation or recovery. Receipts do not create a
   second mutable task store or an independent execution authority.

## Current coverage and gaps

The following describes the implementation at `1601fce`, before this contract
was recorded. Future delivery must replace gaps with concrete acceptance proof.

| Area | Implemented behavior | Remaining requirement |
| --- | --- | --- |
| Runtime | Immutable archive, `-I -S` launcher, version floor, retained recovery snapshots; checkout source is deliberate for self-development. | Preserve the documented invocation boundary and test interpreter changes; a bundled interpreter is not required by this contract. |
| Repository policy | `blackdog.toml` configures handlers; current handler kinds are `blackdog-runtime` and `python-overlay-venv`. | Reviewed, versioned general preparation recipe and explicit unknown-requirement reporting. No general language setup engine is shipped. |
| Python dependencies | Optional overlay creates a local venv and points it at primary site-packages. | Shared packages are mutable; no dependency fingerprint binds manifests, lockfiles, configuration and toolchain to reuse. |
| Python source and tools | Plain path entries in some `.pth` files are mapped into the task checkout; missing tool scripts can fall back to primary `bin` symlinks. | Executable editable finders and other mappings are not generally reconstructed. A primary script can retain its primary interpreter; import and entrypoint correctness need representative proof. |
| Nontracked inputs | Git supplies the tracked base; handler-specific setup is available. | No general declared ignored/untracked-input materialization or external requirement coverage. |
| Evidence | Setup receipts record handler probes, blockers and timing; lifecycle recovery reports exact actions. | Full recipe/input/dependency identities, drift applicability and general preparation effect recovery. Existing success is handler readiness, not the stronger target contract. |
| Performance | Published release measurements exercise isolated synthetic repositories and the runtime lifecycle. | Real project dependencies and the same preparation/readiness workload across warm, cold and invalidated reuse. |

The current overlay remains an explicit compatibility handler. Its presence
must not be described as satisfying reproducible preparation, and this contract
does not authorize silently deleting or replacing existing project environments.

## Staged implementation and acceptance

Each stage needs independent review, bounded fault tests and explicit evidence
before its target can be called implemented. Extend existing handler and
lifecycle boundaries; avoid a parallel setup service or a second workflow. P3
remains fixture-only until P4 recovery and concurrent publication proof passes.

| Stage | Bounded delivery | Required proof |
| --- | --- | --- |
| P1: Runtime assurance | Preserve the immutable release and supported isolated invocation contract; add regression coverage and separately evaluate early rejection of missing isolation flags. | Poison `PYTHONPATH`, current directory, user/system site hooks and unrelated installed packages; imports still use the archive. Exercise explicit interpreter selection, supported upgrades, unsupported versions, and the unisolated explicit-Python invocation boundary. |
| P2: Recipe admission | Define the minimal versioned recipe and typed preparation receipt, with reviewed discovery proposals and exact attempt/source binding. | Missing, unknown, incompatible or changed policy cannot produce ready. Declared nontracked inputs have verified identity and bounded materialization; undeclared files remain untouched. Existing handler policy upgrades are explicit and preserve history. |
| P3: Verified preparation | Implement qualified dependency reuse and worktree-local execution; extend the existing handler boundary with the minimal required language adapters. | Python, Node and mixed-language fixtures pass the same application checks with cold and warm caches. Editable imports, console entrypoints and generated/native outputs belong to the task checkout. Manifest, lockfile, configuration, toolchain and artifact mismatches invalidate reuse. |
| P4: Recovery and concurrency | Make preparation publication, drift checks and effect recovery evidence-based. | Change a manifest, lockfile or declared input during setup; no mixed-input readiness is published. Interrupt before/after effects and receipt publication; retry either verifies/resumes safely or reports indeterminate. Competing tasks never consume partial artifacts or mutate one another. Primary and active-task environments remain intact during cache rebuild or replacement. |
| P5: Measurement and rollout | Adopt reviewed recipes in representative projects, then compare equivalent preparation work. | Report fixtures, exact revisions, dependencies, interpreter/toolchain, cache state, readiness assertions, sample counts, missing/failed samples and per-phase distributions. Measure cold, warm, mismatch/rebuild and recovery paths; disclose regressions and limits. Rollout keeps a recoverable prior configuration and runtime. |

Representative fixtures must include an installed Python package with a real
editable import and console entrypoint, a locked Node application with build
and test commands, and a mixed project whose generated output connects both.
Include a declared ignored input, unavailable required external input, and
source modifications that distinguish the task checkout from primary. Exercise
paths with spaces, environment drift after initial readiness, concurrent reuse,
failed dependency installation and recovery after the task workspace disappears.
Dependency acquisition uses pinned public fixtures or verified local artifacts;
benchmark reports must distinguish downloads from local cache preparation.

Acceptance records must say what was observed and what remains unknown. A warm
cache speedup is useful only when it preserves the same readiness criteria as a
cold build. Neither a successful synthetic lifecycle nor an existing handler
receipt closes the unimplemented preparation requirements above.
