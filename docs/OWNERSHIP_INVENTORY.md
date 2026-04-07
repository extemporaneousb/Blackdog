# File Ownership Inventory

This inventory maps the current files under `src/blackdog`, `tests`, `docs`, and `editors/emacs` to one primary owner for extraction planning.

## Ownership buckets

- `core`: reusable primitives, schemas, persistence helpers, and parsing/config code that should stay light on product policy and UI knowledge.
- `proper`: Blackdog-specific runtime policy and orchestration, including WTAM, supervisor behavior, scaffolding, planning semantics, and product rules.
- `viewers`: read-mostly surfaces that render or browse runtime state and artifacts.
- `clients`: operator entrypoints and integrations that call into the runtime or viewers.

Use a primary owner even when a file is mixed today. The migration note calls out the seam when a file should be split later.

## Concentration points

The main extraction risks are still concentrated in a small set of files:

| File | Lines | Current owner | Why it is risky |
| --- | ---: | --- | --- |
| `tests/test_blackdog_cli.py` | 9737 | `clients` | One integration-heavy file currently carries almost every CLI/runtime contract. |
| `src/blackdog/supervisor.py` | 3106 | `proper` | Mixes scheduling, launch protocol, landing recovery, and run reporting. |
| `src/blackdog/ui.py` | 2661 | `viewers` | Owns the HTML/snapshot read model but still reaches into broad runtime data. |
| `src/blackdog/backlog.py` | 2586 | `proper` | Mixes parsing, planning policy, prompt shaping, telemetry enrichment, and view projection. |
| `src/blackdog/cli.py` | 2287 | `clients` | Large command surface wrapping almost every subsystem. |

## Runtime package

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `src/blackdog/__init__.py` | `clients` | Package/version export only. | Keep thin; do not let product logic accumulate here. |
| `src/blackdog/__main__.py` | `clients` | Python entrypoint wrapper around the CLI. | Keep as a veneer over the extracted CLI package. |
| `src/blackdog/backlog.py` | `proper` | Owns Blackdog task semantics, planning, prompt shaping, and status/view assembly. | Split later into `core` parsing/state math, `proper` policy/task shaping, and viewer-facing projection helpers. |
| `src/blackdog/cli.py` | `clients` | Shell-facing command registry and argument plumbing for the whole product. | Shrink by pushing command business logic deeper into owned modules. |
| `src/blackdog/config.py` | `core` | Defines profile loading, path resolution, defaults, and low-level repo config rules. | Good early extraction candidate. |
| `src/blackdog/scaffold.py` | `proper` | Blackdog-specific bootstrap/refresh/update workflow and managed-skill scaffolding. | Split rendering hooks away from project bootstrap if viewers are extracted separately. |
| `src/blackdog/skill_cli.py` | `clients` | Compatibility CLI for project-local skill scaffold operations. | Keep as a thin client over scaffold/proper behavior. |
| `src/blackdog/store.py` | `core` | Low-level file locking, JSON persistence, inbox/results/thread/state storage helpers. | Strong `core` anchor; keep free of supervisor/UI policy. |
| `src/blackdog/supervisor.py` | `proper` | Owns delegated execution policy, child protocol, recovery, and landing semantics. | Extract only after `core` state/persistence seams are stable. |
| `src/blackdog/ui.py` | `viewers` | Builds the static snapshot/HTML read surface and render helpers. | Preserve read-only contract; minimize direct policy knowledge over time. |
| `src/blackdog/ui.css` | `viewers` | Styling for the static HTML board. | Move with the HTML viewer package. |
| `src/blackdog/worktree.py` | `proper` | WTAM branch/worktree lifecycle and landing contract. | Keep adjacent to supervisor/proper unless a general workspace library emerges. |

## Tests

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `tests/test_blackdog_cli.py` | `clients` | Primary executable contract suite is written against CLI behavior and therefore crosses every subsystem. | Split by owned surface as extraction progresses: `core` unit tests, `proper` workflow tests, `viewers` render tests, and thinner CLI integration coverage. |

## Docs

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `docs/ARCHITECTURE.md` | `proper` | Describes runtime boundaries, supervisor model, WTAM flow, and product structure. | Keep updated as extraction changes package boundaries. |
| `docs/CHARTER.md` | `proper` | Product intent and delivery direction for Blackdog itself. | Remains product-level unless a separate platform charter appears. |
| `docs/CLI.md` | `clients` | Documents command-entry contracts and operator-facing behavior. | Reorganize by extracted client surfaces when commands move. |
| `docs/EMACS.md` | `clients` | Describes the Emacs workbench as an operator integration over snapshot/CLI surfaces. | Keep viewer sections aligned with any Emacs package split. |
| `docs/FILE_FORMATS.md` | `core` | Canonical contract for persisted runtime artifacts and profile schema. | Treat as the source of truth before moving code. |
| `docs/INDEX.md` | `clients` | Reader entrypoint into the doc set. | Include extraction docs here so operators can find the new package map. |
| `docs/INTEGRATION.md` | `clients` | Host-repo adoption and integration guidance. | Update when bootstrap/update flows move. |

## Emacs package

`editors/emacs/lisp/blackdog-core.el` is a client-side helper layer for Emacs. It is not the same thing as extraction `core`.

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `editors/emacs/README.md` | `clients` | Entrypoint and install notes for the Emacs integration. | Keep paired with the Emacs package root. |
| `editors/emacs/templates/blackdog-spec.md` | `clients` | Operator-facing authoring template for spec drafting. | Move with any spec-drafting client package. |
| `editors/emacs/test/blackdog-test.el` | `clients` | End-to-end coverage for the Emacs integration surface. | Split by viewer/client modules only after the package boundary is stable. |
| `editors/emacs/lisp/blackdog.el` | `clients` | Top-level Emacs entrypoint and command map. | Keep thin over smaller client/viewer modules. |
| `editors/emacs/lisp/blackdog-core.el` | `clients` | Shared Emacs-side process/JSON/path helpers. | Rename or document carefully if extracted alongside Python `core` to avoid confusion. |
| `editors/emacs/lisp/blackdog-codex.el` | `clients` | Codex session integration and conversation workflow. | Separate cleanly from snapshot viewers; it is a client integration. |
| `editors/emacs/lisp/blackdog-magit.el` | `clients` | Magit bridge for worktree/task navigation. | Depends on product task metadata but is still an operator client. |
| `editors/emacs/lisp/blackdog-spec.el` | `clients` | Structured task drafting and launch workflow. | Keep with other write-capable operator clients. |
| `editors/emacs/lisp/blackdog-telemetry.el` | `clients` | Supervisor control and telemetry monitor, including async launches. | Mixed file today; if split later, keep control actions client-side and rendering helpers viewer-side. |
| `editors/emacs/lisp/blackdog-thread.el` | `clients` | Conversation-thread authoring, preview, and task creation. | Client workflow layer over Blackdog thread/runtime contracts. |
| `editors/emacs/lisp/blackdog-artifacts.el` | `viewers` | Read-only artifact navigation helpers. | Move with task/result/run readers. |
| `editors/emacs/lisp/blackdog-dashboard.el` | `viewers` | Magit-style snapshot dashboard. | Viewer surface over snapshot; avoid embedding write logic. |
| `editors/emacs/lisp/blackdog-results.el` | `viewers` | Result list browser. | Keep read-only and snapshot-driven. |
| `editors/emacs/lisp/blackdog-runs.el` | `viewers` | Supervisor run browser. | Keep grouped with other artifact viewers. |
| `editors/emacs/lisp/blackdog-search.el` | `viewers` | Navigation/search over tasks, artifacts, and repo files. | Viewer-adjacent browse layer; avoid adding state transitions here. |
| `editors/emacs/lisp/blackdog-task.el` | `viewers` | Task detail reader and artifact browser. | Today it also launches actions; if extracted, keep reader/rendering here and move command glue outward. |

## Migration notes

1. Extract `core` first.
   `src/blackdog/config.py`, `src/blackdog/store.py`, and the pure parsing/schema portions of `src/blackdog/backlog.py` are the safest reusable substrate.
2. Keep `proper` responsible for policy-heavy workflows.
   `src/blackdog/supervisor.py`, `src/blackdog/worktree.py`, `src/blackdog/scaffold.py`, and the product-policy parts of `src/blackdog/backlog.py` should move together or behind stable `core` interfaces.
3. Separate viewers by read contract, not by implementation language.
   The static HTML board (`src/blackdog/ui.py`, `src/blackdog/ui.css`) and the Emacs read surfaces can evolve independently once they consume a stable snapshot/result/artifact contract.
4. Slim clients last.
   `src/blackdog/cli.py`, `src/blackdog/skill_cli.py`, `src/blackdog/__main__.py`, and the Emacs command modules should become veneers over extracted subsystems instead of staying as mixed orchestration layers.
5. Split tests with the code, not before.
   The current single-file CLI suite is useful as a top-level safety net; add narrower owner-specific tests before carving it apart.
