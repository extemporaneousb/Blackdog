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

For a code-derived visual companion, open
[docs/architecture-diagrams.html](docs/architecture-diagrams.html).
That page is generated from the checked-out Python sources by
`blackdog architecture-docs` and is intended to make the current module
map, runtime artifact surface, event flow, and actor/worktree contract
easier to scan than prose alone.

## System shape

Blackdog is a repo-versioned backlog system built in four package
surfaces:

| Package | Target distribution name | Purpose | Must not absorb |
| --- | --- | --- | --- |
| `blackdog_core` | `blackdog-core` | Durable backlog/runtime contract shared by every Blackdog client. | Supervisor policy, HTML/view composition, skill scaffolding, editor UX, tracked-install registry logic, and other product-specific behavior. |
| `blackdog` | `blackdog` | The shipped Blackdog product built on top of `blackdog_core`. | Canonical state formats or thin CLI adapter code. |
| `blackdog_cli` | `blackdog-cli` | Thin parser and command adapter behind the `blackdog` executable. | Business logic, runtime ownership, or product policy. |
| `extensions` | n/a | Optional adapters and operator surfaces that consume documented Blackdog contracts. | Canonical runtime state formats or write-path rules. |

The critical architectural rule is that `blackdog_core` defines the
contract and every other layer composes it. Blackdog should not keep
redefining that contract in prompt text, static HTML, supervisor code,
or editor tools.

## Core runtime charter

`blackdog_core` is the narrow runtime export. It exists so humans,
`blackdog`, `blackdog_cli`, and future adapters can all depend on the same
dependency-light contract.

Core owns:

- repo-local profile loading and path resolution
- canonical backlog parsing and validation
- derived read-only snapshot builders over backlog/state/record inputs
- canonical artifact contracts for backlog, state, events, inbox, and
  task results
- deterministic state transitions for approval, task claims, inbox
  replay, comments, and structured results
- task selection, dependency checks, and plan interpretation
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
belong in `blackdog_core`.

## Blackdog

`blackdog` owns the shipped Blackdog product experience layered
on top of `blackdog_core`.

`blackdog` owns:

- workflow orchestration such as `worktree start|land|cleanup`
- the branch-backed worktree lifecycle plus supervisor run/recovery
  state machines layered on top of the core runtime
- supervisor orchestration and delegated child protocol
- bootstrap, refresh, update, and project-local skill generation
- prompt/tune/report helpers
- Blackdog-owned conversation threads
- tracked-install registry and unattended-tuning aggregation
- snapshot composition and the rendered static HTML board

`blackdog` must remain a consumer of `blackdog_core`, not a replacement
for it. Product code can choose defaults and compose workflows, but the
durable artifact contract and basic state semantics still come from
`blackdog_core`.

## blackdog_cli

`blackdog_cli` exists so the executable stays thin.

It owns:

- `argparse` command tree construction
- CLI help text and option parsing
- command-to-library dispatch into `blackdog_core` and `blackdog`
- the single public `blackdog` command inventory; it does not hide or
  filter commands based on internal package ownership

It does not own:

- persistent data structures
- backlog/state semantics
- snapshot semantics
- supervisor or worktree policy

For Blackdog's own repo, this layer is currently manual-first when the
work touches implementation code: operators should be able to continue
through the direct claim/worktree/result/land flow even when supervisor
hardening is still in progress.

## Extensions

`extensions` own optional operator-specific integrations.

Examples:

- the Emacs workbench under `extensions/emacs/`
- future IDE integrations
- alternate viewers or reporting surfaces
- host-specific wrappers that consume documented Blackdog behavior

Extensions may depend on documented `blackdog` commands or on
stable artifact contracts, but they must not redefine durable state or
quietly become a second control plane.

## Runtime composition

Blackdog's runtime model is simple once the layers are separated:

1. A repo-local `blackdog.toml` file defines the project identity and
   resolves the shared control-root paths.
2. `blackdog_core` interprets the backlog, reads and writes canonical
   runtime artifacts, enforces deterministic state transitions, and
   exposes readonly WTAM facts.
3. `blackdog` turns those primitives into user-facing commands,
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

### Blackdog product surfaces

- CLI workflow composition
- branch-backed worktree lifecycle orchestration, including workspace
  role and landing-readiness state
- supervisor run artifacts, normalized run-state semantics, and
  recovery behavior
- Blackdog-owned conversation threads and tracked-install registry data
- generated skills and bootstrap scaffolding
- snapshot composition and rendered HTML

### CLI adapter surface

- the `blackdog` executable
- `python -m blackdog_cli`
- parser wiring that dispatches to `blackdog_core` and `blackdog`

### Optional extension surfaces

- editor integrations
- alternate monitoring or reporting tools
- host-specific wrappers around documented commands or artifact reads

## Transitional package map

The source tree is still transitional. Ownership follows the charter,
not whichever top-level module currently contains the code.

| Target layer | Transitional homes today |
| --- | --- |
| `blackdog_core` | `profile.py`, `backlog.py`, `state.py`, `snapshot.py` |
| `blackdog` | `scaffold.py`, `worktree.py`, `supervisor.py`, `supervisor_policy.py`, `tuning.py`, `conversations.py`, `installs.py`, `board.py`, `ui.css` |
| `blackdog_cli` | `main.py`, `__main__.py` |
| `extensions` | `extensions/emacs/` and future optional adapter packages |

## Migration rules

Extraction work should follow these rules:

1. Move reusable file-contract and state-transition logic toward
   `blackdog_core`.
2. Keep scaffold, supervisor, render, conversation, tracked-install,
   and policy behavior in `blackdog`.
3. Keep only parsing and dispatch in `blackdog_cli`.
4. Push editor- or environment-specific behavior into `extensions`
   instead of expanding the product runtime.
5. Preserve the `blackdog` executable name and durable artifact
   contracts while internal module ownership changes.
6. Update this document, [docs/CLI.md](docs/CLI.md), and
   [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md) when a boundary would
   otherwise become ambiguous.

## Non-goals

The narrowed runtime charter explicitly rejects these moves:

- treating the whole `blackdog` package as if it were all `core`
- moving HTML, supervisor, prompt, scaffold, or skill logic into
  `blackdog_core`
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

Route the change to `blackdog_core`, `blackdog`, or
`extensions` based on that answer. If the answer is not obvious from the
current docs, fix the docs before broadening the code.
