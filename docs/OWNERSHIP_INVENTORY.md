# File Ownership Inventory

This inventory maps the current files under `src/blackdog_core`, `src/blackdog`,
`src/blackdog_cli`, `tests`, `docs`, and `extensions/emacs` to one primary
owner for extraction planning.

## Ownership buckets

- `blackdog_core`: durable runtime contract, canonical state, and snapshot math.
- `blackdog`: shipped product behavior layered on top of `blackdog_core`.
- `blackdog_cli`: thin command adapter around `blackdog_core` and `blackdog`.
- `extensions`: optional operator integrations such as Emacs.

Use a primary owner even when a file is mixed today. The migration note calls out the seam when a file should be split later.

## Concentration points

The main extraction risks are still concentrated in a small set of files:

| File | Lines | Current owner | Why it is risky |
| --- | ---: | --- | --- |
| `tests/test_blackdog_cli.py` | 9737 | `blackdog_cli` | One integration-heavy file currently carries almost every CLI/runtime contract. |
| `src/blackdog/supervisor.py` | 3106 | `blackdog` | Mixes scheduling, launch protocol, landing recovery, and run reporting. |
| `src/blackdog/board.py` | 2661 | `blackdog` | Owns the HTML/board projection and still reaches into broad runtime data. |
| `src/blackdog_core/backlog.py` | 2586 | `blackdog_core` | Owns durable backlog parsing, task semantics, plan interpretation, and runtime snapshot builders. |
| `src/blackdog_cli/main.py` | 2287 | `blackdog_cli` | Large command surface wrapping almost every subsystem. |

## Python packages

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `src/blackdog_core/backlog.py` | `blackdog_core` | Owns durable backlog/task/plan semantics plus runtime snapshot construction. | Keep as the contract layer; do not let product policy drift back in. |
| `src/blackdog_core/profile.py` | `blackdog_core` | Defines profile loading, path resolution, defaults, and low-level repo config rules. | Good early extraction candidate. |
| `src/blackdog_core/state.py` | `blackdog_core` | Low-level file locking, JSON persistence, inbox/results/state storage helpers. | Strong contract anchor; keep free of supervisor/UI policy. |
| `src/blackdog_core/snapshot.py` | `blackdog_core` | Public read-only snapshot/artifact entrypoint. | Keep as the stable loader/builder surface for other layers. |
| `src/blackdog/__init__.py` | `blackdog` | Package/version export only. | Keep thin; do not let product logic accumulate here. |
| `src/blackdog/scaffold.py` | `blackdog` | Bootstrap/refresh/update workflow and managed-skill scaffolding. | Keep as product orchestration over the core runtime contract. |
| `src/blackdog/worktree.py` | `blackdog` | WTAM branch/worktree lifecycle and landing contract. | Keep adjacent to supervisor unless a general workspace library emerges. |
| `src/blackdog/supervisor.py` | `blackdog` | Delegated execution policy, child protocol, recovery, and landing semantics. | Extract only after core state/persistence seams are stable. |
| `src/blackdog/board.py` | `blackdog` | Static board projection and HTML rendering helpers. | Preserve the board as a product surface over `runtime_snapshot`. |
| `src/blackdog/ui.css` | `blackdog` | Styling for the static HTML board. | Move with the board renderer if the viewer surface is split later. |
| `src/blackdog/conversations.py` | `blackdog` | Blackdog-owned saved conversation threads and task linkage. | Keep product-owned and out of `blackdog_core`. |
| `src/blackdog/installs.py` | `blackdog` | Machine-local tracked-install registry handling. | Keep as product host-management state, not core runtime contract. |
| `src/blackdog_cli/main.py` | `blackdog_cli` | Shell-facing command registry and argument plumbing for the whole product. | Shrink by pushing command business logic deeper into owned modules. |
| `src/blackdog_cli/__main__.py` | `blackdog_cli` | Python entrypoint for `python -m blackdog_cli`. | Keep as a veneer over the extracted CLI package. |

## Tests

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `tests/test_blackdog_cli.py` | `blackdog_cli` | Primary executable contract suite is written against CLI behavior and therefore crosses every subsystem. | Split by owned surface as extraction progresses: `blackdog_core` unit tests, `blackdog` workflow tests, board/render tests, and thinner CLI integration coverage. |

## Docs

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `docs/ARCHITECTURE.md` | `blackdog` | Describes runtime boundaries, supervisor model, WTAM flow, and product structure. | Keep updated as extraction changes package boundaries. |
| `docs/CHARTER.md` | `blackdog` | Product intent and delivery direction for Blackdog itself. | Remains product-level unless a separate platform charter appears. |
| `docs/CLI.md` | `blackdog_cli` | Documents command-entry contracts and operator-facing behavior. | Reorganize by extracted command surfaces when commands move. |
| `docs/EMACS.md` | `extensions` | Describes the Emacs workbench as an operator integration over snapshot/CLI surfaces. | Keep viewer sections aligned with any Emacs package split. |
| `docs/FILE_FORMATS.md` | `blackdog_core` | Canonical contract for persisted runtime artifacts and profile schema. | Treat as the source of truth before moving code. |
| `docs/INDEX.md` | `blackdog_cli` | Reader entrypoint into the doc set. | Include extraction docs here so operators can find the new package map. |
| `docs/INTEGRATION.md` | `blackdog_cli` | Host-repo adoption and integration guidance. | Update when bootstrap/update flows move. |
| `docs/TARGET_MODEL.md` | `blackdog` | Freezes the future runtime-kernel model, object vocabulary, and open design decisions. | Update before broadening task, attempt, planning, or supervisor semantics. |

## Emacs package

`extensions/emacs/lisp/blackdog-core.el` is a client-side helper layer for Emacs. It is not the same thing as extraction `core`.

| File | Owner | Why | Migration note |
| --- | --- | --- | --- |
| `extensions/emacs/README.md` | `extensions` | Entrypoint and install notes for the Emacs integration. | Keep paired with the Emacs package root. |
| `extensions/emacs/templates/blackdog-spec.md` | `extensions` | Operator-facing authoring template for spec drafting. | Move with any spec-drafting client package. |
| `extensions/emacs/test/blackdog-test.el` | `extensions` | End-to-end coverage for the Emacs integration surface. | Split by viewer/client modules only after the package boundary is stable. |
| `extensions/emacs/lisp/blackdog.el` | `extensions` | Top-level Emacs entrypoint and command map. | Keep thin over smaller integration modules. |
| `extensions/emacs/lisp/blackdog-core.el` | `extensions` | Shared Emacs-side process/JSON/path helpers. | Rename or document carefully if extracted alongside Python `blackdog_core` to avoid confusion. |
| `extensions/emacs/lisp/blackdog-codex.el` | `extensions` | Codex session integration and conversation workflow. | Separate cleanly from snapshot viewers; it is an integration layer. |
| `extensions/emacs/lisp/blackdog-magit.el` | `extensions` | Magit bridge for worktree/task navigation. | Depends on product task metadata but is still an operator integration. |
| `extensions/emacs/lisp/blackdog-spec.el` | `extensions` | Structured task drafting and launch workflow. | Keep with other write-capable operator integrations. |
| `extensions/emacs/lisp/blackdog-telemetry.el` | `extensions` | Supervisor control and telemetry monitor, including async launches. | Mixed file today; if split later, keep control actions outside `blackdog_core`. |
| `extensions/emacs/lisp/blackdog-thread.el` | `extensions` | Conversation-thread authoring, preview, and task creation. | Client workflow layer over Blackdog thread/runtime contracts. |
| `extensions/emacs/lisp/blackdog-artifacts.el` | `extensions` | Read-only artifact navigation helpers. | Move with task/result/run readers. |
| `extensions/emacs/lisp/blackdog-dashboard.el` | `extensions` | Magit-style snapshot dashboard. | Viewer surface over snapshot; avoid embedding write logic. |
| `extensions/emacs/lisp/blackdog-results.el` | `extensions` | Result list browser. | Keep read-only and snapshot-driven. |
| `extensions/emacs/lisp/blackdog-runs.el` | `extensions` | Supervisor run browser. | Keep grouped with other artifact viewers. |
| `extensions/emacs/lisp/blackdog-search.el` | `extensions` | Navigation/search over tasks, artifacts, and repo files. | Viewer-adjacent browse layer; avoid adding state transitions here. |
| `extensions/emacs/lisp/blackdog-task.el` | `extensions` | Task detail reader and artifact browser. | Today it also launches actions; if extracted, keep reader/rendering here and move command glue outward. |

## Migration notes

1. Extract `blackdog_core` first.
   `src/blackdog_core/profile.py`, `src/blackdog_core/state.py`, and the pure parsing/schema portions of `src/blackdog_core/backlog.py` are the safest reusable substrate.
2. Keep `blackdog` responsible for policy-heavy workflows.
   `src/blackdog/supervisor.py`, `src/blackdog/worktree.py`, `src/blackdog/scaffold.py`, and the product-policy parts of `src/blackdog_core/backlog.py` should move together or behind stable `core` interfaces.
3. Separate viewers by read contract, not by implementation language.
   The static HTML board (`src/blackdog/board.py`, `src/blackdog/ui.css`) and the Emacs read surfaces can evolve independently once they consume a stable snapshot/result/artifact contract.
4. Slim `blackdog_cli` last.
   `src/blackdog_cli/main.py`, `src/blackdog_cli/__main__.py`, and the Emacs command modules should become veneers over extracted subsystems instead of staying as mixed orchestration layers.
5. Split tests with the code, not before.
   The current single-file CLI suite is useful as a top-level safety net; add narrower owner-specific tests before carving it apart.
