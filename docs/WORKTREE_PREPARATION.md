# Runtime and worktree preparation contract

This is the accepted architecture contract and the supported preparation
boundary. Runtime isolation is implemented under the invocation conditions
below. An opt-in, schema-1 `worktree-preparation` handler now delivers reviewed
recipes, worktree-owned setup, verified reuse in the same checkout, and fresh
readiness checks. The broader acceptance stages remain explicit below; this
bounded delivery does not establish a universal environment or shared cache.

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

## Shipped recipe contract

Add an explicit handler to the repository's reviewed `blackdog.toml`. Existing
handlers and receipts remain compatible; install/update does not replace a
`python-overlay-venv` configuration automatically. This example assumes the
named tracked scripts implement the repository's dependency installation and
readiness assertions. Set the exact tool probe output for the supported local
toolchain before reviewing and committing the recipe.

```toml
[[handlers]]
id = "prepare"
kind = "worktree-preparation"
schema_version = 1
revision = "reviewed-v1"
tracked_inputs = ["pyproject.toml", "requirements.lock", "tools/prepare.py", "tools/check_ready.py"]
outputs = [".VE", ".prepared"]
timeout_seconds = 300

[[handlers.tools]]
name = "python"
executable = "python3"
version_args = ["--version"]
version = "Python 3.11.0"

[[handlers.setup]]
name = "venv"
argv = ["{python}", "-m", "venv", ".VE"]

[[handlers.setup]]
name = "install"
argv = ["{worktree}/.VE/bin/python", "tools/prepare.py"]

[[handlers.checks]]
name = "imports-and-entrypoints"
argv = ["{worktree}/.VE/bin/python", "tools/check_ready.py"]
```

- `schema_version = 1` and a nonempty reviewed `revision` are required. Unknown
  preparation fields fail admission. `enabled` and `depends_on` retain their
  existing handler meanings.
- `tracked_inputs` lists exact regular-file paths whose change requires new
  setup. `blackdog.toml` is always included. Manifests, lockfiles, installation
  scripts, local wheels/tarballs and generated-output inputs belong here when
  they affect setup. There is no dependency discovery or implicit globbing.
- `outputs` lists nonoverlapping, ignored, untracked directories owned by this
  handler in the task checkout. Existing outputs without complete ownership
  evidence block setup. They may not contain tracked files or escape through a
  symlink. Output links may target other inventoried outputs, exact tracked
  regular files, or exact qualified tool executables. A tracked directory does
  not qualify its ignored contents. Blackdog never copies a virtual environment or attaches a primary
  mutable package directory.
- Each `tools` entry has `name`, a PATH executable name, `version_args`, and an
  exact expected `version` output. Receipts bind executable bytes, resolved
  path, platform/machine and probe output. Recipes can include ABI/compiler
  details in that probe; transitive tool libraries are not automatically hashed.
- `setup` and `checks` are ordered arrays of `{name, argv}` tables. No shell is
  added by Blackdog. `{worktree}` and declared tool names such as `{python}`
  expand to absolute paths. The first argument must select a declared tool or
  an executable under an owned output directory. Readiness checks must leave
  prepared outputs unchanged.
- Optional `[[handlers.inputs]]` entries require exactly `source`,
  `destination`, `sha256`, and `mode`. The source is one explicit ignored,
  untracked regular file relative to the primary checkout; the destination is
  inside an owned task output. SHA-256 is required, and mode is one of `0o600`,
  `0o644`, `0o700`, or `0o755`. Receipts record identity and permissions, never
  contents. Missing inputs block. No directory copy, private-file discovery,
  secret injection, or external-service adapter is provided.

Commands run with the task checkout as cwd, a fresh environment, PATH derived
from declared tool directories, and HOME/TMPDIR inside the first owned output.
Inherited private variables, Python import overrides and user package-manager
configuration are not passed through. Version probes use disposable scratch
HOME/cwd because even a version query can initialize package-manager state.
Pip defaults to no-index and npm to offline operation. Python bytecode and the
[Node module compile cache](https://nodejs.org/download/release/v26.5.1/docs/api/module.html)
are disabled to keep verification from changing dependency artifacts.

`timeout_seconds` is a shared wall-clock budget for command execution and
version probes, from 1 to 3600 seconds. Each Git inspection is separately
bounded to 30 seconds; filesystem snapshots have file/count/byte bounds rather
than a hard total wall-clock deadline. Command output is discarded. Receipts
retain the observed phase and, for setup/readiness commands, step name; failures
include the exit or timeout category. Version output is capped at
4096 bytes. Remaining process-group members are terminated and block success.
Reviewed commands remain trusted repository code: this is not an OS sandbox,
a network firewall, or proof against commands that deliberately escape their
process group or write elsewhere.

### Readiness and recovery

`task begin` resolves the recipe from the selected task checkout and claims its
canonical attempt with a pending setup receipt before executing recipe effects.
Private intent is published before creating owned outputs. Successful setup
records source HEAD/tree and the tracked working-file snapshot, recipe and
setup-input identities, toolchain, complete output-inventory digest, checks,
and the owning task/attempt. Input/source/tool/output observations must still
match before readiness is published.

Completed setup is reused only in the same worktree. An exact begin retry
verifies the recipe, declared inputs, toolchain and output inventory, and runs
readiness checks again. Unrelated tracked source edits get a fresh source-bound
receipt without reinstalling dependencies. Changed setup inputs, tools,
outputs, or policy block reuse and require inspection or a new attempt;
Blackdog does not delete or repair the existing environment automatically.
Exact verification with unchanged ready identities leaves canonical state and
events unchanged. A receipt describes its recorded source snapshot; read-only
reports do not run preparation commands or establish perpetual readiness.

Failed, timed-out, or interrupted setup retains its canonical active attempt,
worktree and private intent. An intent without completed evidence is
indeterminate; retries never replay arbitrary setup commands. A completed
private receipt can be verified after interruption before canonical publication.
Missing evidence or a disappeared worktree blocks and uses the existing task
recovery/close protocol. Known blocked preparation remains blocked in task
show/recover and cannot start landing. There is no second task scheduler or
setup-service authority.

## Current coverage and residuals

| Area | Delivered | Remaining boundary |
| --- | --- | --- |
| Runtime | Immutable isolated archive and retained recovery runtime. | Documented system interpreter/native-library trust remains. |
| Policy | Explicit versioned recipe with strict fields, selected-checkout resolution and declared input coverage. | Reviewed discovery/proposal generation; unknown requirements remain unknown. |
| Python | Fresh owned venv, verified local wheel, real editable installation and console script tested against task source. | Broader build backends, native extensions, external dependency sets and platform matrix. |
| Node/mixed | Locked dependency-free npm application builds/tests offline; Python generates the Node build input. | External Node package dependency acceptance and broader package-manager/toolchain proof. |
| Reuse | Exact worktree-local output identity plus fresh readiness checks; no mutable primary attachment. | Shared immutable dependency cache and cross-task artifact reuse. |
| Recovery | Canonical claim before effects, compare-and-set receipt updates, repairable append-once events, blocked incomplete effects and serialized publication. | Automatic recovery of arbitrary installers is deliberately unsupported. |
| Inputs | Explicit pinned ignored regular-file copies; missing inputs and source/input/tool/output drift block. | Tracked symlinks/submodules, external services, secrets and snapshots beyond supported bounds. |
| Measurement | One descriptive cold/verified-worktree mixed fixture sample uses the same checks and local artifacts. | Representative real-project adoption and repeated cold/warm/mismatch/recovery distributions. |

Input/source/output files are limited to 64 MiB each, tool executables to
512 MiB, and a snapshot to 30,000 entries and 512 MiB. These are admission
limits, not assurances for larger repositories. The compatibility overlay
handler still points at mutable primary dependencies and does not inherit the
stronger recipe assurance. No existing environment is silently migrated.

## Staged implementation and acceptance

Each stage needs independent review, bounded fault tests and explicit evidence
before its target can be called implemented. Extend existing handler and
lifecycle boundaries; avoid a parallel setup service or a second workflow.
The shipped slice proves its specific local fixtures and fault cases; it does
not close every criterion in these broader stages.

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
