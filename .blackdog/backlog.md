# Blackdog Backlog

Project: `Blackdog`
Repo root: `/Users/bullard/Work/Blackdog`
Generated: `2026-03-13T00:15:42-07:00`
Target branch: `main`
Target commit: `9b6427346dffc277f18f273b921a75d1f745f934`
Profile: `/Users/bullard/Work/Blackdog/blackdog.toml`
State file: `/Users/bullard/Work/Blackdog/.blackdog/backlog-state.json`
Events file: `/Users/bullard/Work/Blackdog/.blackdog/events.jsonl`
Inbox file: `/Users/bullard/Work/Blackdog/.blackdog/inbox.jsonl`
Results dir: `/Users/bullard/Work/Blackdog/.blackdog/task-results`
HTML file: `/Users/bullard/Work/Blackdog/.blackdog/backlog-index.html`

## Objectives

- OBJ-1: Stable repo-versioned backlog core
- OBJ-2: Strong AI-agent interaction surfaces
- OBJ-3: Easy project onboarding via skill scaffold

## Push Objective

- Ship the first usable local backlog runtime with structured state, events, inbox, results, rendering, and project-local skill generation.

## Non-Negotiables

- Keep the runtime repo-versioned and dependency-light.
- Preserve local-first operation and avoid premature multi-user complexity.

## Evidence Requirements

- Every follow-up task should point at concrete code, docs, or UX gaps.
- Behavior changes should update docs and tests in the same slice.

## Release Gates

- Blackdog can scaffold itself and a target project through the CLI.
- Blackdog can render one backlog control page from live repo artifacts.

## Recent Work Snapshot

- Initial backlog scaffold created.

## Alignment Notes

- This backlog is optimized for local AI-assisted development and repo-versioned coordination.

## Inventory Map

- Use `blackdog` for durable state transitions, `blackdog-skill` for project scaffold and skill generation, and `.blackdog/` for the local backlog artifact set.

## Epic Map

- Add epics as the backlog matures.

## Ranked Top 3

- Add tasks, then keep this list current.

## Lane Plan

- Add lanes and waves as tasks are introduced.

```json backlog-plan
{
  "epics": [
    {
      "id": "epic-runtime-usability",
      "title": "Runtime usability",
      "task_ids": [
        "BLACK-decc668b7e"
      ]
    },
    {
      "id": "epic-agent-runtime",
      "title": "Agent runtime",
      "task_ids": [
        "BLACK-fa0d5ad18d"
      ]
    },
    {
      "id": "epic-project-onboarding",
      "title": "Project onboarding",
      "task_ids": [
        "BLACK-afcc9ebf68"
      ]
    }
  ],
  "lanes": [
    {
      "id": "lane-usability-lane",
      "title": "Usability lane",
      "task_ids": [
        "BLACK-decc668b7e"
      ],
      "wave": 0
    },
    {
      "id": "lane-agent-runtime-lane",
      "title": "Agent runtime lane",
      "task_ids": [
        "BLACK-fa0d5ad18d"
      ],
      "wave": 0
    },
    {
      "id": "lane-migration-lane",
      "title": "Migration lane",
      "task_ids": [
        "BLACK-afcc9ebf68"
      ],
      "wave": 1
    }
  ]
}
```

### BLACK-decc668b7e - Add per-task detail HTML pages linked from the backlog index

Why it matters: The control page is useful, but task-level drilldown is still too shallow for long-lived runs and agent handoffs.
Evidence: The current HTML output is a single page with no per-task detail view.
Affected paths: `src/blackdog/backlog.py`, `src/blackdog/scaffold.py`, `docs/ARCHITECTURE.md`.

```json backlog-task
{
  "id": "BLACK-decc668b7e",
  "title": "Add per-task detail HTML pages linked from the backlog index",
  "bucket": "html",
  "priority": "P2",
  "risk": "medium",
  "effort": "M",
  "packages": [],
  "paths": [
    "src/blackdog/backlog.py",
    "src/blackdog/scaffold.py",
    "docs/ARCHITECTURE.md"
  ],
  "checks": [
    "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'"
  ],
  "docs": [
    "AGENTS.md",
    "docs/INDEX.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI.md",
    "docs/FILE_FORMATS.md"
  ],
  "objective": "OBJ-1",
  "domains": [
    "html",
    "results"
  ],
  "requires_approval": false,
  "approval_reason": "",
  "safe_first_slice": "Generate one task detail page from the existing view model and link it from the main index."
}
```

### BLACK-fa0d5ad18d - Add a local supervisor runner with structured child-run result capture

Why it matters: Blackdog has claims, inbox, and task-result files, but it does not yet include a repo-local supervisor loop that can drive child agents against that contract.
Evidence: Today the repo ships the backlog runtime but not a matching launcher for autonomous child execution.
Affected paths: `src/blackdog/cli.py`, `src/blackdog/store.py`, `docs/ARCHITECTURE.md`, `docs/CLI.md`.

```json backlog-task
{
  "id": "BLACK-fa0d5ad18d",
  "title": "Add a local supervisor runner with structured child-run result capture",
  "bucket": "integration",
  "priority": "P2",
  "risk": "high",
  "effort": "M",
  "packages": [],
  "paths": [
    "src/blackdog/cli.py",
    "src/blackdog/store.py",
    "docs/ARCHITECTURE.md",
    "docs/CLI.md"
  ],
  "checks": [
    "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'"
  ],
  "docs": [
    "AGENTS.md",
    "docs/INDEX.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI.md",
    "docs/FILE_FORMATS.md"
  ],
  "objective": "OBJ-2",
  "domains": [
    "cli",
    "inbox",
    "results"
  ],
  "requires_approval": false,
  "approval_reason": "",
  "safe_first_slice": "Run one child task end-to-end through a local supervisor command that writes a task-result file and updates inbox state."
}
```

### BLACK-afcc9ebf68 - Add a migration helper for importing legacy markdown backlogs into Blackdog

Why it matters: Adoption will be slower if every repo has to re-enter an existing backlog by hand.
Evidence: The current repo can scaffold a fresh backlog, but it cannot import an existing branch backlog format.
Affected paths: `src/blackdog/backlog.py`, `src/blackdog/cli.py`, `docs/FILE_FORMATS.md`, `docs/CLI.md`.

```json backlog-task
{
  "id": "BLACK-afcc9ebf68",
  "title": "Add a migration helper for importing legacy markdown backlogs into Blackdog",
  "bucket": "integration",
  "priority": "P2",
  "risk": "medium",
  "effort": "M",
  "packages": [],
  "paths": [
    "src/blackdog/backlog.py",
    "src/blackdog/cli.py",
    "docs/FILE_FORMATS.md",
    "docs/CLI.md"
  ],
  "checks": [
    "PYTHONPATH=src python3 -m unittest discover -s tests -p 'test_*.py'"
  ],
  "docs": [
    "AGENTS.md",
    "docs/INDEX.md",
    "docs/ARCHITECTURE.md",
    "docs/CLI.md",
    "docs/FILE_FORMATS.md"
  ],
  "objective": "OBJ-3",
  "domains": [
    "cli",
    "docs"
  ],
  "requires_approval": false,
  "approval_reason": "",
  "safe_first_slice": "Parse one legacy backlog file into Blackdog task and lane structures without mutating the source file."
}
```
