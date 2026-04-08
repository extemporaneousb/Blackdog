# Architecture

This document describes the narrowed runtime charter and the layer
boundaries Blackdog is extracting toward.

Use it to answer:

- what belongs in the durable runtime contract
- what Blackdog owns as shipped product behavior on top of that
  contract
- what should remain optional adapter or operator surface

Do not use this document as the detailed command or file-schema
reference. Keep those details in:

- [docs/CLI.md](docs/CLI.md) for command ownership and command-level
  behavior
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md) for canonical artifact
  contracts
- [docs/BOUNDARIES.md](docs/BOUNDARIES.md) for the frozen extraction
  charter

## System shape

Blackdog is a repo-versioned backlog system built in three layers:

| Layer | Purpose | Must not absorb |
| --- | --- | --- |
| `blackdog.core` | Durable backlog/runtime contract shared by every Blackdog client. | Supervisor policy, HTML/view composition, skill scaffolding, editor UX, and other product-specific behavior. |
| `blackdog.proper` | The shipped Blackdog product built on top of `core`. | Optional editor or host-specific integrations. |
| `extensions` | Optional adapters and operator surfaces that consume documented Blackdog contracts. | Canonical runtime state formats or write-path rules. |

The critical architectural rule is that `core` defines the contract and
every other layer composes it. Blackdog should not keep redefining that
contract in prompt text, static HTML, supervisor code, or editor tools.

## Core runtime charter

`blackdog.core` is the narrow runtime export. It exists so humans,
Blackdog proper, and future adapters can all depend on the same
dependency-light contract.

Core owns:

- repo-local profile loading and path resolution
- canonical backlog parsing and validation
- canonical artifact contracts for backlog, state, events, inbox, and
  task results
- deterministic state transitions for approval, task claims, inbox
  replay, comments, and structured results
- task selection, dependency checks, plan interpretation, and stable
  read models over the artifact set
- WTAM safety facts such as workspace-contract inspection,
  primary-worktree detection, and other readonly invariants shared by
  higher layers

Core does not own:

- worktree lifecycle orchestration such as start, land, and cleanup
- supervisor launch policy, child prompts, or recovery workflows
- prompt/tune/report policy
- static HTML rendering or browser-facing interaction design
- project skill scaffolding, bootstrap, refresh, or update flows
- editor-specific conversation or task UX

The core contract is intentionally boring: durable files, deterministic
state, and stable inspection semantics. If a feature needs Blackdog
defaults, a branded workflow, or an operator-facing UI, it does not
belong in `core`.

## Blackdog proper

`blackdog.proper` owns the shipped Blackdog product experience layered
on top of `core`.

Blackdog proper owns:

- the `blackdog`, `blackdog-skill`, and `python -m blackdog`
  entrypoints
- workflow orchestration such as `worktree start|land|cleanup`
- the branch-backed worktree lifecycle plus supervisor run/recovery
  state machines layered on top of the core runtime
- supervisor orchestration and delegated child protocol
- bootstrap, refresh, update, and project-local skill generation
- prompt/tune/report helpers
- snapshot composition and the rendered static HTML board

Blackdog proper must remain a consumer of `core`, not a replacement for
it. Product code can choose defaults and compose workflows, but the
durable artifact contract and basic state semantics still come from
`core`.

For Blackdog's own repo, this layer is currently manual-first when the
work touches implementation code: operators should be able to continue
through the direct claim/worktree/result/land flow even when supervisor
hardening is still in progress.

## Extensions

`extensions` own optional operator-specific integrations.

Examples:

- the Emacs workbench under `editors/emacs/`
- future IDE integrations
- alternate viewers or reporting surfaces
- host-specific wrappers that consume documented Blackdog behavior

Extensions may depend on documented `blackdog.proper` commands or on
stable artifact contracts, but they must not redefine durable state or
quietly become a second control plane.

## Runtime composition

Blackdog's runtime model is simple once the layers are separated:

1. A repo-local `blackdog.toml` file defines the project identity and
   resolves the shared control-root paths.
2. `blackdog.core` interprets the backlog, reads and writes canonical
   runtime artifacts, enforces deterministic state transitions, and
   exposes readonly WTAM facts.
3. `blackdog.proper` turns those primitives into user-facing commands,
   workflow orchestration, supervisor behavior, and rendered views.
4. `extensions` consume the documented command and artifact surfaces
   without owning the underlying runtime contract.

The default shared control root still matters architecturally because it
lets every worktree see the same backlog runtime state. But the exact
artifact names, schemas, and CLI surfaces belong in the dedicated
reference docs rather than being duplicated here.

## Runtime and product surfaces

The narrow boundary is easiest to keep clear when the main surfaces are
classified explicitly:

### Core-owned contract surfaces

- `blackdog.toml` as the repo-local entrypoint
- the canonical backlog/state/event/inbox/result artifact set under the
  resolved control root
- deterministic plan and task-state semantics over that artifact set
- WTAM inspection facts used by higher layers to decide whether
  implementation work is safe

### Blackdog-product surfaces

- CLI workflow composition
- branch-backed worktree lifecycle orchestration, including workspace
  role and landing-readiness state
- supervisor run artifacts, normalized run-state semantics, and
  recovery behavior
- generated skills and bootstrap scaffolding
- snapshot composition and rendered HTML

### Optional extension surfaces

- editor integrations
- alternate monitoring or reporting tools
- host-specific wrappers around documented commands or artifact reads

## Transitional package map

The source tree is still transitional. Ownership follows the charter,
not whichever top-level module currently contains the code.

| Target layer | Transitional homes today |
| --- | --- |
| `blackdog.core` | `core/config.py`, `core/backlog.py`, `core/store.py`, plus readonly WTAM contract facts that are still mixed into `worktree.py` |
| `blackdog.proper` | `cli.py`, `skill_cli.py`, `scaffold.py`, `supervisor.py`, `ui.py`, `ui.css`, and the orchestration-heavy parts of `worktree.py` |
| `extensions` | `editors/emacs/` and future optional adapter packages |

Compatibility matters during extraction:

- `blackdog.cli`
- `blackdog.skill_cli`
- `blackdog.config`
- `blackdog.backlog`
- `blackdog.store`
- `blackdog.worktree`
- `blackdog.supervisor`
- `blackdog.ui`
- `blackdog.scaffold`

Those module paths remain stable entrypoints or shims until the new
internal homes fully replace them. The boundary matters more than the
interim filename.

## Migration rules

Extraction work should follow these rules:

1. Move reusable file-contract and state-transition logic toward
   `blackdog.core`.
2. Keep CLI, scaffold, supervisor, render, and policy behavior in
   `blackdog.proper`.
3. Push editor- or environment-specific behavior into `extensions`
   instead of expanding the product runtime.
4. Preserve stable executable names and durable artifact contracts while
   internal module ownership changes.
5. Update this document, [docs/CLI.md](docs/CLI.md), and
   [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md) when a boundary would
   otherwise become ambiguous.

## Non-goals

The narrowed runtime charter explicitly rejects these moves:

- treating the whole `blackdog` package as if it were all `core`
- moving HTML, supervisor, prompt, scaffold, or skill logic into
  `blackdog.core`
- promoting optional extensions into canonical write-path owners
- using current mixed file placement as proof of long-term ownership
- changing durable artifact or executable contracts just because the
  internal package layout is being cleaned up

## Decision rule for new work

Before adding or moving code, ask:

1. Does this define or enforce the canonical backlog/runtime contract?
2. Does it instead compose that contract into shipped Blackdog product
   behavior?
3. Is it optional operator or host integration that could be removed
   without breaking the runtime contract?

Route the change to `blackdog.core`, `blackdog.proper`, or
`extensions` based on that answer. If the answer is not obvious from the
current docs, fix the docs before broadening the code.
