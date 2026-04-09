# Extraction Audit

This note audits the current extraction risk for the viewer, editor, and supervisor surfaces that still live close to the Blackdog core runtime.

## Scope

- Viewer and snapshot renderer in `src/blackdog/board.py`
- Host bootstrap and skill/html scaffold in `src/blackdog/scaffold.py`
- Supervisor launcher and child protocol in `src/blackdog/supervisor.py`
- Emacs entrypoint in `extensions/emacs/lisp/blackdog.el`

## Current boundary

Blackdog's docs already describe the HTML board, snapshot contract, and delegated supervisor flow as normal operating surfaces, not optional side artifacts. In practice, those surfaces are still implemented inside `src/blackdog/`, so extracting them is not just a file move. It is a contract split.

## Findings

### 1. Viewer extraction is medium-high risk because snapshot shaping and HTML rendering are fused.

- `src/blackdog/board.py:1502` builds the full UI snapshot by loading backlog, state, inbox, events, task results, threads, tracked installs, worktree contract data, and git metadata in one place.
- `src/blackdog/board.py:1793` renders the static HTML page from that same snapshot and inlines both CSS and the snapshot JSON payload into the output file.
- `src/blackdog/board.py:1515` through `src/blackdog/board.py:1525` add git-origin and commit metadata directly inside the snapshot builder, which means the viewer surface is currently responsible for repo introspection as well as presentation.

Risk:

- A clean extraction cannot move only the HTML template. The data-shaping contract the page depends on is also in `board.py`.
- Any snapshot-schema drift will affect both the board and any other readers that assume the current payload shape.
- The current module mixes durable-contract work with browser-only concerns such as markdown rendering, link shaping, and embedded response truncation.

Recommended split:

- Keep a stable snapshot builder contract in core.
- Move HTML templating, CSS packaging, and browser-only formatting to a viewer adapter.
- Treat git metadata enrichment as an explicit optional presenter concern instead of an implicit core obligation.

### 2. Scaffold extraction is medium risk because host bootstrap regenerates viewer and skill surfaces together.

- `src/blackdog/scaffold.py:219` through `src/blackdog/scaffold.py:261` bootstrap the project skill and immediately render the HTML board.
- `src/blackdog/scaffold.py:315` through `src/blackdog/scaffold.py:320` make `render_project_html()` the shared bridge between scaffold flows and the viewer implementation.
- `src/blackdog/scaffold.py:466` through `src/blackdog/scaffold.py:714` generate project skill text and refresh the managed scaffold as part of the shipped host contract.

Risk:

- Moving the viewer or supervisor out of core without updating scaffold generation will leave newly bootstrapped repos with stale instructions.
- The scaffold layer currently assumes the board is a built-in Blackdog surface, not a pluggable adapter.
- Skill generation hardcodes user-facing descriptions of the board and supervisor behavior, so extraction affects host-facing docs as well as code.

Recommended split:

- Reduce scaffold's responsibility to generating contract references and invoking registered adapters.
- Keep the repo-contract text in sync with whichever viewer and supervisor adapters are installed.
- Treat HTML rendering as an optional capability behind a narrow hook instead of a direct import from core.

### 3. Supervisor extraction is high risk because launch policy, child protocol, and run artifacts are all implemented in one `blackdog` product module.

- `src/blackdog/supervisor.py:89` through `src/blackdog/supervisor.py:137` define the child prompt contract inline.
- `src/blackdog/supervisor.py:140` through `src/blackdog/supervisor.py:192` define the generated `blackdog-child` helper inline.
- `src/blackdog/supervisor.py:1872` through `src/blackdog/supervisor.py:1938` derive the child prompt from task payload, worktree contract state, and workspace-specific rules.
- `src/blackdog/supervisor.py:2440` through `src/blackdog/supervisor.py:2615` prepare worktrees, write prompt/stdout/stderr/metadata artifacts, send inbox instructions, set launch env vars, and spawn the child process.
- `src/blackdog/supervisor.py:288` calls `render_project_html(profile)` from supervisor code, so run-state changes currently reach back into the viewer implementation directly.

Risk:

- The supervisor is not only a scheduler. It owns the launch transport, prompt format, protocol helper, and run-artifact layout.
- The child workspace contract is currently "git-worktree only" and the launch flow assumes an exec-capable Codex runtime.
- Run-artifact filenames like `prompt.txt`, `stdout.log`, and `metadata.json` are already part of documented operating surfaces, so changing them is a compatibility change, not an internal refactor.

Recommended split:

- Keep task selection, claim/release/complete semantics, and run-artifact schema in core.
- Move launcher-specific prompt building, process spawning, and runtime selection into a supervisor adapter.
- Replace direct `render_project_html()` calls with a post-state-change render hook so the core does not import the viewer.

### 4. Editor extraction is medium risk because the Emacs entrypoint is thin, but it anchors several Blackdog-specific surface assumptions.

- `extensions/emacs/lisp/blackdog.el:14` through `extensions/emacs/lisp/blackdog.el:24` pull in Blackdog-specific dashboard, results, task, run, Codex, thread, spec, and telemetry modules from one package entrypoint.
- `extensions/emacs/lisp/blackdog.el:27` through `extensions/emacs/lisp/blackdog.el:54` hardcode inspection paths under `.codex/skills/blackdog/...`.
- `extensions/emacs/lisp/blackdog.el:85` through `extensions/emacs/lisp/blackdog.el:120` publish a top-level command map that treats dashboard, telemetry, worktree launch, and supervisor controls as one integrated product surface.
- The actual telemetry module calls CLI `supervise status|report|recover|run` directly and opens run artifacts by path, so the editor layer depends on stable command names and artifact layout even when the entrypoint itself stays thin.

Risk:

- Emacs is already an adapter in directory structure, but it still assumes Blackdog-owned command names, skill paths, and run-artifact conventions.
- A viewer or supervisor extraction that changes those paths or command outputs will break Emacs without any code move in `extensions/emacs/`.
- The hardcoded skill inspection path is a concrete compatibility trap if the skill name or installation location changes.

Recommended split:

- Treat Emacs as an external adapter that depends only on stable CLI and snapshot contracts.
- Introduce compatibility aliases for skill-inspection paths or expose those paths via CLI/config instead of hardcoding them.
- Keep run-artifact names and JSON status/report shapes stable while adapters are moved.

## Migration order

1. Freeze the durable contracts first.
   Define which snapshot fields, run-artifact filenames, child-prompt facts, and CLI JSON payloads are part of the supported interface.

2. Split viewer rendering from snapshot shaping.
   Move the HTML template, CSS, and markdown-to-HTML presentation helpers behind a viewer adapter while keeping the current snapshot payload intact.

3. Replace direct render imports with hooks.
   `blackdog/scaffold.py` and `blackdog/supervisor.py` should not import the viewer implementation directly once the adapter exists.

4. Extract supervisor launch transport after the artifact contract is stable.
   The scheduler and task lifecycle can stay in core while prompt generation, protocol helper creation, and process spawning move outward.

5. Migrate editor integrations last.
   Emacs should follow the stabilized CLI/snapshot surface, not lead the contract split.

## Suggested acceptance criteria for a follow-up extraction task

- Core can build the canonical snapshot without importing HTML/CSS rendering code.
- Supervisor state transitions can run without importing viewer code.
- Viewer rendering can be disabled or replaced without affecting backlog/state/result semantics.
- Emacs can continue to inspect skill files, supervisor runs, and task artifacts through stable interfaces after the move.
