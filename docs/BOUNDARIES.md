# Core Boundaries

This document freezes the ownership boundary Blackdog should use for the
current remodel. It defines three layers:

- `core`: stable backlog/runtime primitives that should remain dependency-light
  and reusable outside the Blackdog product surface
- `blackdog proper`: the Blackdog product contract that composes core
  primitives into the shipped CLI and repo workflow
- `extensions`: optional adapters and operator-specific surfaces that depend on
  the Blackdog product contract but are not part of the minimal Blackdog
  runtime

The current source tree is transitional. File placement does not yet prove
ownership; this charter does.

## Why Freeze This Now

Current package and CLI surfaces mix durable runtime state, prompt/skill
guidance, supervisor behavior, rendered HTML, and editor integration under one
namespace. That makes extraction work ambiguous. The remodel needs one fixed
answer to:

- what must stay small and stable
- what Blackdog itself owns as product behavior
- what should move behind extension or adapter boundaries

## Layer Definitions

### `core`

`core` owns the durable, local-first backlog/runtime contract.

Allowed responsibilities:

- repo profile loading and path resolution
- canonical backlog parsing and validation
- canonical file formats for backlog, state, events, inbox, results, and
  related runtime artifacts
- deterministic state transitions for claims, release, completion, comments,
  approvals, and structured results
- task selection, dependency checks, and plan interpretation
- stable WTAM primitives for worktree contract inspection and branch-backed task
  workspace lifecycle
- pure or near-pure snapshot/read-model builders over the canonical artifact
  set

Must stay out of `core`:

- editor-specific workflows
- prompt-authoring ergonomics beyond minimal contract text
- Codex launch policy and child-agent UX details
- HTML presentation concerns beyond read-model data that a renderer can consume
- repo bootstrap/scaffold opinions that are specific to Blackdog as a product
- host-specific integrations that assume one UI or one operator environment

Design rules:

- prefer Python stdlib
- keep contracts explicit and file-backed
- optimize for reuse by multiple Blackdog frontends or adapters
- treat CLI-independent library behavior as the source of truth

### `blackdog proper`

`blackdog proper` owns the shipped Blackdog product experience built on top of
`core`.

Allowed responsibilities:

- the `blackdog` and `blackdog-skill` CLIs
- repo bootstrap and refresh flows
- project-local skill generation
- prompt/tune/report helpers that express the Blackdog operating contract
- the static HTML board renderer and view composition
- supervisor orchestration, delegated child protocol, and launch contract
- Blackdog-specific packaging and default policy choices

Must stay out of `blackdog proper`:

- editor-specific UI logic
- host-repo custom automation that is not part of the shared Blackdog product
- one-off adapters that can live as consumers of the CLI or file contract

Design rules:

- compose `core`; do not hide or replace it
- keep product defaults explicit in docs and profile config
- treat generated skills, HTML, and supervisor artifacts as product surfaces,
  not `core` primitives

### `extensions`

`extensions` own optional operator surfaces and adapters that consume Blackdog
through the CLI, stable files, or other documented product contracts.

Allowed responsibilities:

- Emacs workbench and editor integrations
- future IDE or editor plugins
- monitoring, reporting, or visualization tools that are not the shipped static
  board
- wrapper scripts or host-repo helpers built on documented CLI behavior
- future adapter packages that add environment-specific launch or review flows

Rules for extensions:

- they may depend on `blackdog proper`
- they must not redefine durable runtime state formats
- they should prefer documented CLI and snapshot contracts over private imports
- they should be removable without breaking the minimal Blackdog runtime

## Current Surface Classification

The current package layout is still mixed, but the ownership target is already
clear:

- `core` target surface: profile/path resolution, backlog parsing, state/event
  persistence, task/result/inbox contracts, worktree contract inspection, and
  stable snapshot data
- `blackdog proper` target surface: CLI commands, bootstrap/refresh, project
  skill generation, HTML rendering, tune/prompt/report output, and supervisor
  orchestration
- `extensions` target surface: `docs/EMACS.md` and the `editors/emacs/`
  package, plus future editor or host-specific adapters

When a module currently mixes these concerns, extraction should move the code
to the correct layer instead of expanding the mixed surface.

## Extraction Boundaries

The remodel should apply these rules:

1. Move reusable file-contract and state-transition logic toward `core`.
2. Keep Blackdog-specific CLI, scaffold, supervisor, and HTML policy in
   `blackdog proper`.
3. Do not promote editor integrations or future adapters into `core` just
   because they are useful for dogfooding.
4. If a feature needs Blackdog defaults, generated skills, or operator-facing
   launch behavior, it belongs in `blackdog proper`, not `core`.
5. If a feature can be removed without breaking canonical backlog/runtime
   artifacts or WTAM semantics, it is a candidate for `extensions`.

## Phase Boundaries

### Phase 0: Charter Freeze

Freeze the vocabulary and ownership rules in docs. Do not use current file
placement as an argument for what belongs in `core`.

Exit criteria:

- this charter is committed
- README and architecture docs use the same three-layer language
- new backlog work can classify itself against these boundaries

### Phase 1: Core Extraction

Consolidate the dependency-light backlog/runtime primitives into an explicit
`core` surface.

Target outcomes:

- canonical file contracts and state transitions are isolated from Blackdog
  product UI/policy code
- WTAM primitives and snapshot builders have a stable library home
- product-facing modules depend on `core` instead of re-owning those rules

### Phase 2: Blackdog Proper Consolidation

Keep Blackdog's shipped product surface coherent on top of `core`.

Target outcomes:

- CLI/scaffold/supervisor/render flows are documented as `blackdog proper`
- Blackdog-specific policy and default behavior are easier to change without
  destabilizing `core`
- product docs describe Blackdog as a composition over `core`, not as the core
  itself

### Phase 3: Extension Separation

Keep optional integrations behind explicit adapter boundaries.

Target outcomes:

- editor and environment-specific integrations depend on documented product
  contracts
- optional surfaces can evolve or ship independently without changing the
  durable runtime contract
- new adapters have a clear placement rule before code is added

## Decision Rule For New Work

Before adding or moving code, ask:

1. Does this change define or enforce the canonical backlog/runtime artifact
   contract?
2. Does it instead express Blackdog's shipped product behavior on top of that
   contract?
3. Is it optional operator or host integration that could sit outside the
   minimal runtime?

Route the work to `core`, `blackdog proper`, or `extensions` based on that
answer, and update docs when the boundary would otherwise be ambiguous.
