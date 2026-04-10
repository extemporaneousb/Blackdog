from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import argparse
import ast
import html as html_lib
import importlib
import importlib.util
import json
import re


ARCHITECTURE_REPORT_SCHEMA_VERSION = 1
DEFAULT_ARCHITECTURE_DOC = Path("docs/architecture-diagrams.html")

LAYER_CLI = "blackdog_cli"
LAYER_CORE = "blackdog_core"
LAYER_PRODUCT = "blackdog"
LAYER_EXTENSION = "extensions"
LAYER_ORDER = (LAYER_CLI, LAYER_CORE, LAYER_PRODUCT)
LAYER_COLORS = {
    LAYER_CLI: ("#eff6ff", "#1d4ed8"),
    LAYER_CORE: ("#ecfdf5", "#047857"),
    LAYER_PRODUCT: ("#fff7ed", "#c2410c"),
    LAYER_EXTENSION: ("#f5f3ff", "#6d28d9"),
}

PACKAGE_ROOTS = (
    "blackdog_cli",
    "blackdog_core",
    "blackdog",
)

ARTIFACT_FIELDS: dict[str, dict[str, str]] = {
    "profile_file": {"label": "blackdog.toml", "surface": "repo contract"},
    "backlog_file": {"label": "backlog.md", "surface": "runtime contract"},
    "state_file": {"label": "backlog-state.json", "surface": "runtime contract"},
    "events_file": {"label": "events.jsonl", "surface": "runtime contract"},
    "inbox_file": {"label": "inbox.jsonl", "surface": "runtime contract"},
    "results_dir": {"label": "task-results/", "surface": "runtime contract"},
    "threads_dir": {"label": "threads/", "surface": "blackdog product"},
    "html_file": {"label": "<project>-backlog.html", "surface": "blackdog product"},
    "supervisor_runs_dir": {"label": "supervisor-runs/", "surface": "blackdog product"},
}

WRITE_HINTS = {
    "append_event",
    "append_jsonl",
    "atomic_write_text",
    "mkdir",
    "record_comment",
    "record_task_result",
    "resolve_message",
    "save_state",
    "save_tracked_installs",
    "send_message",
    "touch",
    "unlink",
    "write_text",
}
READ_HINTS = {
    "exists",
    "glob",
    "iterdir",
    "load_backlog",
    "load_events",
    "load_inbox",
    "load_jsonl",
    "load_profile",
    "load_runtime_artifacts",
    "load_state",
    "load_task_results",
    "load_thread",
    "load_tracked_installs",
    "open",
    "read_bytes",
    "read_text",
}
WRITER_PREFIXES = (
    "add_",
    "append_",
    "bootstrap_",
    "claim_",
    "cleanup_",
    "complete_",
    "create_",
    "land_",
    "record_",
    "refresh_",
    "release_",
    "remove_",
    "render_project_",
    "reset_",
    "resolve_",
    "save_",
    "scaffold_",
    "send_",
    "sync_",
    "update_",
    "write_",
)
READER_PREFIXES = (
    "build_",
    "classify_",
    "current_",
    "default_",
    "find_",
    "list_",
    "load_",
    "next_",
    "render_",
    "summary_",
)
WRITER_FUNCTIONS = {"refresh_project_scaffold", "render_project_html"}

PREFERRED_DIAGRAM_MODULES = (
    "blackdog_cli.main",
    "blackdog_core.profile",
    "blackdog_core.backlog",
    "blackdog_core.state",
    "blackdog_core.snapshot",
    "blackdog.scaffold",
    "blackdog.board",
    "blackdog.worktree",
    "blackdog.supervisor",
    "blackdog.conversations",
    "blackdog.tuning",
    "blackdog.installs",
)

MODULE_SUMMARY_OVERRIDES = {
    "blackdog.architecture": "Analyzes the checked-out source tree and renders the maintainer-facing architecture overview HTML.",
    "blackdog.board": "Builds the board snapshot and renders the shipped static HTML view from runtime artifacts, git metadata, installs, and conversations.",
    "blackdog.conversations": "Stores Blackdog-owned conversation threads and links thread context back to backlog tasks and recorded results.",
    "blackdog.installs": "Tracks machine-local host-repo installations so one Blackdog checkout can observe or update multiple repos.",
    "blackdog.scaffold": "Bootstraps and refreshes repo-local Blackdog installations, managed skill files, profile defaults, and initial artifact layout.",
    "blackdog.supervisor": "Runs delegated child execution on top of backlog selection, state tracking, and WTAM worktree management.",
    "blackdog.supervisor_policy": "Defines child prompt construction, launch defaults, and reasoning/launcher overrides for supervisor runs.",
    "blackdog.tuning": "Builds prompt/tune analysis from repo history and can seed follow-up tasks when workflow signals suggest a gap.",
    "blackdog.worktree": "Implements the WTAM branch-backed worktree lifecycle, landing safety checks, and worktree-state helpers.",
    "blackdog_cli.main": "Defines the argparse command tree for the `blackdog` executable and dispatches every subcommand into blackdog_core or blackdog.",
    "blackdog_core.backlog": "Parses backlog markdown, validates task payloads and plans, builds runtime snapshots, and selects runnable work.",
    "blackdog_core.profile": "Loads `blackdog.toml`, resolves shared-control-root paths, and defines the repo profile/path contracts every layer consumes.",
    "blackdog_core.snapshot": "Re-exports the readonly snapshot and summary builders that other layers use to inspect runtime state.",
    "blackdog_core.state": "Owns canonical mutable state, append-only event/inbox/result records, and replay/normalization of claim and approval state machines.",
}

MODULE_READ_HINTS = {
    "blackdog.architecture": "Read this when the maintainer overview itself is wrong, too thin, or out of date with the checked-out code.",
    "blackdog.board": "Read this when the static board HTML, embedded snapshot, or UI-facing task/result rendering changes.",
    "blackdog.conversations": "Read this when thread storage, task linkage, or result mirroring into conversations changes.",
    "blackdog.installs": "Read this when host-repo discovery, tracked install persistence, or observe/update flows change.",
    "blackdog.scaffold": "Read this when bootstrap, refresh, update-repo, or managed skill generation changes.",
    "blackdog.supervisor": "Read this when delegated execution, run-state semantics, recovery, or child result handling changes.",
    "blackdog.supervisor_policy": "Read this when launch prompts, Codex launch overrides, or reasoning-effort policy changes.",
    "blackdog.tuning": "Read this when tune/prompt analysis or task-seeding policy changes.",
    "blackdog.worktree": "Read this when WTAM safety, worktree lifecycle, landing behavior, or branch/worktree contract changes.",
    "blackdog_cli.main": "Read this when the CLI surface, help text, argument parsing, or command dispatch changes.",
    "blackdog_core.backlog": "Read this when task parsing, plan semantics, runnable-task selection, or runtime snapshot composition changes.",
    "blackdog_core.profile": "Read this when profile keys, path resolution, default routes, or repo-level contract wiring changes.",
    "blackdog_core.snapshot": "Read this when callers need a readonly runtime view without re-owning backlog or state semantics.",
    "blackdog_core.state": "Read this when claim/approval/inbox/result persistence or validation semantics change.",
}

ARTIFACT_EXPLANATIONS = {
    "profile_file": "Repo-local control plane: this tells every layer where the shared control root lives and what defaults the repo expects.",
    "backlog_file": "The human-authored work graph. Task narrative, plan structure, priorities, paths, checks, and objectives live here.",
    "state_file": "The canonical mutable state machine snapshot for approvals and task claims after replay and reconcile.",
    "events_file": "Append-only operational history. Read this when you need to understand how current state was reached.",
    "inbox_file": "Append-only control messages for supervisor/child coordination and operator instructions.",
    "results_dir": "Structured task closeout evidence, including validations, residual risk, and landed diff artifacts.",
    "threads_dir": "Blackdog-owned freeform conversation history linked back to tasks and results.",
    "html_file": "The rendered backlog board humans open in a browser.",
    "supervisor_runs_dir": "Per-run delegated execution artifacts: prompts, stdout/stderr, metadata, and status.",
}


class ArchitectureError(RuntimeError):
    pass


@dataclass(frozen=True)
class DataclassFacts:
    name: str
    fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "fields": list(self.fields)}


@dataclass(frozen=True)
class ClassFacts:
    name: str
    bases: tuple[str, ...]
    fields: tuple[str, ...]
    methods: tuple[str, ...]
    is_dataclass: bool
    docstring: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "bases": list(self.bases),
            "fields": list(self.fields),
            "methods": list(self.methods),
            "is_dataclass": self.is_dataclass,
            "docstring": self.docstring,
        }


@dataclass(frozen=True)
class ModuleFacts:
    name: str
    path: str
    layer: str
    is_package: bool
    compatibility_shim: bool
    docstring: str
    imports: tuple[str, ...]
    classes: tuple[str, ...]
    class_facts: tuple[ClassFacts, ...]
    functions: tuple[str, ...]
    dataclasses: tuple[DataclassFacts, ...]
    artifact_reads: tuple[str, ...]
    artifact_writes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "path": self.path,
            "layer": self.layer,
            "is_package": self.is_package,
            "compatibility_shim": self.compatibility_shim,
            "docstring": self.docstring,
            "imports": list(self.imports),
            "classes": list(self.classes),
            "class_facts": [row.to_dict() for row in self.class_facts],
            "functions": list(self.functions),
            "dataclasses": [row.to_dict() for row in self.dataclasses],
            "artifact_reads": list(self.artifact_reads),
            "artifact_writes": list(self.artifact_writes),
        }


def default_architecture_output_path(project_root: Path) -> Path:
    return (project_root / DEFAULT_ARCHITECTURE_DOC).resolve()


def analyze_repo_architecture(project_root: Path) -> dict[str, Any]:
    root = project_root.resolve()
    src_root = root / "src"
    package_roots = [src_root / package_name for package_name in PACKAGE_ROOTS if (src_root / package_name).is_dir()]
    if not package_roots:
        raise ArchitectureError(f"Could not find Blackdog sources under {src_root}")

    module_index: dict[str, Path] = {}
    for package_root in package_roots:
        for path in sorted(package_root.rglob("*.py")):
            module_index[_module_name(src_root, path)] = path

    known_modules = frozenset(module_index)
    modules = [
        _analyze_module(path, module_name=module_name, src_root=src_root, known_modules=known_modules)
        for module_name, path in sorted(module_index.items())
    ]
    non_package_modules = [module for module in modules if not module.is_package]
    module_rows = [module.to_dict() for module in non_package_modules]
    dataclass_rows = [
        {
            "module": module.name,
            "layer": module.layer,
            "name": dataclass_facts.name,
            "fields": list(dataclass_facts.fields),
        }
        for module in non_package_modules
        for dataclass_facts in module.dataclasses
    ]
    compatibility_modules = [
        {
            "module": module.name,
            "path": module.path,
            "target": next((candidate for candidate in module.imports if candidate in known_modules), ""),
        }
        for module in non_package_modules
        if module.compatibility_shim
    ]
    command_surfaces = _extract_command_surfaces(root / "src" / "blackdog_cli" / "main.py")
    commands_by_package = _commands_by_package(command_surfaces)
    cli_inventory = _extract_cli_inventory(command_surfaces)
    artifacts = _artifact_report(non_package_modules)
    extensions = _extension_report(root)
    module_guide = _build_module_guide(module_rows)
    workflows = _workflow_report(cli_inventory, artifacts)
    read_order = _read_order_report(module_rows)
    layer_counts = Counter(module.layer for module in non_package_modules)
    summary = {
        "module_count": len(non_package_modules),
        "class_count": sum(len(module.class_facts) for module in non_package_modules),
        "dataclass_count": len(dataclass_rows),
        "compatibility_module_count": len(compatibility_modules),
        "artifact_surface_count": len(artifacts),
        "command_count": len(command_surfaces),
        "extension_surface_count": len(extensions),
        "layer_counts": {layer: layer_counts.get(layer, 0) for layer in LAYER_ORDER},
    }
    return {
        "schema_version": ARCHITECTURE_REPORT_SCHEMA_VERSION,
        "generated_at": _now_iso(),
        "project_root": str(root),
        "summary": summary,
        "modules": module_rows,
        "module_guide": module_guide,
        "read_order": read_order,
        "diagram_modules": [module.to_dict() for module in _representative_modules(non_package_modules)],
        "dataclasses": dataclass_rows,
        "artifacts": artifacts,
        "command_surfaces": command_surfaces,
        "commands_by_package": commands_by_package,
        "cli_inventory": cli_inventory,
        "workflows": workflows,
        "compatibility_modules": compatibility_modules,
        "extensions": extensions,
    }


def render_architecture_html(report: dict[str, Any], output_path: Path) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = "Blackdog Maintainer Overview"
    summary = report["summary"]
    generated_at = report["generated_at"]
    project_root = report["project_root"]
    command_lines = [
        "./.VE/bin/blackdog architecture-docs --project-root . --output docs/architecture-diagrams.html",
        "PYTHONPATH=src python3 -m blackdog_cli architecture-docs --project-root .",
    ]
    html = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{html_lib.escape(title)}</title>
  <style>
    :root {{
      color-scheme: light;
      --bg: #f7f5ef;
      --panel: #fffdf8;
      --panel-alt: #f2efe5;
      --border: #d8d1c1;
      --text: #1f2933;
      --muted: #52606d;
      --accent: #0f766e;
      --accent-soft: rgba(15, 118, 110, 0.12);
      --code-bg: #f3f4f6;
      --table-border: #e5dfd0;
      --shadow: 0 18px 45px rgba(31, 41, 51, 0.08);
    }}
    * {{ box-sizing: border-box; }}
    body {{
      margin: 0;
      font-family: "Avenir Next", "Segoe UI", sans-serif;
      background:
        radial-gradient(circle at top right, rgba(15, 118, 110, 0.12), transparent 28rem),
        linear-gradient(180deg, #fcfbf6 0%, var(--bg) 100%);
      color: var(--text);
      line-height: 1.55;
    }}
    main {{
      max-width: 1760px;
      margin: 0 auto;
      padding: 2.4rem 1.4rem 3.2rem;
    }}
    header {{
      background: linear-gradient(135deg, #114b5f 0%, #0f766e 56%, #d97706 100%);
      color: white;
      border-radius: 28px;
      padding: 2.1rem 2.2rem;
      box-shadow: var(--shadow);
      position: relative;
      overflow: hidden;
    }}
    header::after {{
      content: "";
      position: absolute;
      inset: auto -10% -35% auto;
      width: 24rem;
      height: 24rem;
      background: radial-gradient(circle, rgba(255, 255, 255, 0.22), rgba(255, 255, 255, 0));
      border-radius: 999px;
    }}
    h1, h2, h3 {{
      font-family: "Iowan Old Style", "Palatino Linotype", serif;
      letter-spacing: -0.02em;
      margin: 0 0 0.4rem;
    }}
    h1 {{ font-size: clamp(2rem, 5vw, 3.2rem); }}
    h2 {{ font-size: 1.65rem; margin-bottom: 0.8rem; }}
    h3 {{ font-size: 1.05rem; margin-bottom: 0.45rem; }}
    p {{
      margin: 0 0 1rem;
      color: var(--muted);
    }}
    .header-grid {{
      display: grid;
      grid-template-columns: minmax(0, 1.35fr) minmax(0, 0.85fr);
      gap: 1.4rem;
      position: relative;
      z-index: 1;
    }}
    .lede {{
      font-size: 1.02rem;
      color: rgba(255, 255, 255, 0.9);
      max-width: 52rem;
    }}
    .meta {{
      display: grid;
      gap: 0.65rem;
      align-content: start;
      justify-items: start;
    }}
    .meta-card {{
      width: 100%;
      background: rgba(255, 255, 255, 0.16);
      border: 1px solid rgba(255, 255, 255, 0.18);
      border-radius: 18px;
      padding: 0.9rem 1rem;
      backdrop-filter: blur(14px);
    }}
    .meta-label {{
      font-size: 0.72rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      opacity: 0.78;
    }}
    .meta-value {{
      font-family: "SFMono-Regular", Menlo, monospace;
      font-size: 0.88rem;
      word-break: break-word;
      margin-top: 0.25rem;
    }}
    .section {{
      margin-top: 1.8rem;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 26px;
      padding: 1.45rem;
      box-shadow: var(--shadow);
    }}
    .section > p:last-child {{
      margin-bottom: 0;
    }}
    .summary-grid {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(12rem, 1fr));
      gap: 0.9rem;
      margin-top: 1.1rem;
    }}
    .summary-card {{
      border-radius: 20px;
      border: 1px solid var(--border);
      background: linear-gradient(180deg, #fffef9, var(--panel-alt));
      padding: 1rem;
    }}
    .summary-value {{
      font-size: 1.9rem;
      font-weight: 700;
      color: var(--text);
      margin-bottom: 0.2rem;
    }}
    .summary-label {{
      font-size: 0.86rem;
      color: var(--muted);
    }}
    .diagram {{
      overflow-x: auto;
      border-radius: 22px;
      border: 1px solid var(--table-border);
      background: #fffdf9;
      padding: 0.8rem;
    }}
    .diagram svg {{
      max-width: 100%;
      height: auto;
    }}
    .caption {{
      margin-top: 0.8rem;
      font-size: 0.92rem;
      color: var(--muted);
    }}
    .subgrid {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 0.8fr);
      gap: 1rem;
      align-items: start;
    }}
    .callout {{
      background: var(--panel-alt);
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 1rem;
    }}
    .two-up {{
      display: grid;
      grid-template-columns: repeat(2, minmax(0, 1fr));
      gap: 1rem;
      align-items: start;
    }}
    .three-up {{
      display: grid;
      grid-template-columns: repeat(3, minmax(0, 1fr));
      gap: 1rem;
      align-items: start;
    }}
    .read-order {{
      display: grid;
      gap: 0.8rem;
      margin: 0;
      padding: 0;
      list-style: none;
    }}
    .read-step {{
      display: grid;
      gap: 0.2rem;
      padding: 0.9rem 1rem;
      border: 1px solid var(--border);
      border-radius: 18px;
      background: linear-gradient(180deg, #fffef9, var(--panel-alt));
    }}
    .read-step strong {{
      color: var(--text);
      font-family: "SFMono-Regular", Menlo, monospace;
      font-size: 0.9rem;
    }}
    .workflow-grid,
    .artifact-grid,
    .module-grid {{
      display: grid;
      gap: 1rem;
    }}
    .workflow-grid {{
      grid-template-columns: repeat(auto-fit, minmax(22rem, 1fr));
    }}
    .artifact-grid {{
      grid-template-columns: repeat(auto-fit, minmax(18rem, 1fr));
    }}
    .module-grid {{
      grid-template-columns: 1fr;
    }}
    .workflow-card,
    .artifact-card,
    .module-card {{
      border: 1px solid var(--border);
      border-radius: 22px;
      background: linear-gradient(180deg, #fffef9, #faf7ef);
      padding: 1rem 1.05rem;
    }}
    .eyebrow {{
      font-size: 0.74rem;
      text-transform: uppercase;
      letter-spacing: 0.08em;
      color: var(--muted);
      margin-bottom: 0.35rem;
    }}
    .step-list,
    .class-list {{
      display: grid;
      gap: 0.75rem;
      margin-top: 0.9rem;
    }}
    .step-row,
    .class-row {{
      border-top: 1px solid var(--table-border);
      padding-top: 0.75rem;
    }}
    .step-row:first-child,
    .class-row:first-child {{
      border-top: none;
      padding-top: 0;
    }}
    .step-command {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 0.14rem 0.55rem;
      background: rgba(29, 78, 216, 0.1);
      color: #1d4ed8;
      font-size: 0.78rem;
      font-family: "SFMono-Regular", Menlo, monospace;
      white-space: nowrap;
    }}
    .module-section {{
      margin-top: 1.2rem;
    }}
    .module-section:first-child {{
      margin-top: 0;
    }}
    .module-header {{
      display: flex;
      align-items: baseline;
      justify-content: space-between;
      gap: 1rem;
      margin-bottom: 0.8rem;
    }}
    .module-meta {{
      color: var(--muted);
      font-size: 0.85rem;
    }}
    .module-title {{
      font-family: "SFMono-Regular", Menlo, monospace;
      font-size: 1rem;
      color: var(--text);
      margin: 0;
      white-space: nowrap;
    }}
    .module-subgrid {{
      display: grid;
      grid-template-columns: minmax(0, 1.2fr) minmax(0, 1fr);
      gap: 1rem;
      align-items: start;
      margin-top: 0.9rem;
    }}
    .table-wrap {{
      overflow-x: auto;
      border: 1px solid var(--table-border);
      border-radius: 18px;
      background: #fffefb;
    }}
    .wide-table {{
      min-width: 1480px;
    }}
    .dense-table {{
      min-width: 980px;
    }}
    .nowrap {{
      white-space: nowrap;
    }}
    .wrap-anywhere {{
      overflow-wrap: anywhere;
    }}
    .muted {{
      color: var(--muted);
    }}
    code, pre {{
      font-family: "SFMono-Regular", Menlo, monospace;
      font-size: 0.88rem;
    }}
    pre {{
      margin: 0;
      white-space: pre-wrap;
      word-break: break-word;
      background: var(--code-bg);
      border-radius: 16px;
      padding: 0.95rem;
      color: var(--text);
      overflow-x: auto;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.91rem;
    }}
    th, td {{
      text-align: left;
      vertical-align: top;
      padding: 0.7rem 0.55rem;
      border-bottom: 1px solid var(--table-border);
    }}
    th {{
      color: var(--muted);
      font-size: 0.78rem;
      text-transform: uppercase;
      letter-spacing: 0.06em;
    }}
    .chips {{
      display: flex;
      flex-wrap: wrap;
      gap: 0.35rem;
      margin-top: 0.35rem;
    }}
    .chip {{
      display: inline-flex;
      align-items: center;
      border-radius: 999px;
      padding: 0.18rem 0.55rem;
      background: var(--accent-soft);
      color: var(--accent);
      font-size: 0.78rem;
      font-family: "SFMono-Regular", Menlo, monospace;
    }}
    details {{
      margin-top: 1rem;
      border: 1px solid var(--border);
      border-radius: 18px;
      padding: 0.9rem 1rem;
      background: #fffefb;
    }}
    summary {{
      cursor: pointer;
      font-weight: 600;
    }}
    .mono {{
      font-family: "SFMono-Regular", Menlo, monospace;
      overflow-wrap: anywhere;
    }}
    @media (max-width: 920px) {{
      .header-grid,
      .subgrid,
      .two-up,
      .three-up,
      .module-subgrid {{
        grid-template-columns: 1fr;
      }}
      main {{
        padding-left: 0.9rem;
        padding-right: 0.9rem;
      }}
      header {{
        padding: 1.6rem;
      }}
    }}
  </style>
</head>
<body>
  <main>
    <header>
      <div class="header-grid">
        <div>
          <div class="meta-label">Generated Maintainer Handbook</div>
          <h1>{html_lib.escape(title)}</h1>
          <p class="lede">This page is generated from the checked-out Python sources and the live argparse tree. It is meant to answer the maintainer questions that matter first: which modules own the runtime contract, how backlog/state/events/results actually flow through the system, what each class carries, and which CLI surfaces dispatch into which package.</p>
        </div>
        <div class="meta">
          <div class="meta-card">
            <div class="meta-label">Generated At</div>
            <div class="meta-value">{html_lib.escape(generated_at)}</div>
          </div>
          <div class="meta-card">
            <div class="meta-label">Project Root</div>
            <div class="meta-value">{html_lib.escape(project_root)}</div>
          </div>
          <div class="meta-card">
            <div class="meta-label">Refresh Command</div>
            <div class="meta-value">{html_lib.escape(command_lines[0])}</div>
          </div>
        </div>
      </div>
    </header>

    <section class="section">
      <h2>Observed Surface</h2>
      <p>The analyzer counts Python modules and classes, walks the runtime artifact readers and writers, and derives the command tree from the current argparse parser instead of a hand-maintained table.</p>
      <div class="summary-grid">
        {_overview_cards(summary)}
      </div>
      <div class="subgrid" style="margin-top: 1rem;">
        <div class="callout">
          <h3>Package Counts</h3>
          <div class="chips">{_layer_chips(summary["layer_counts"])}</div>
        </div>
        <div class="callout">
          <h3>Refresh</h3>
          <pre>{html_lib.escape("\n".join(command_lines))}</pre>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Start Here</h2>
      <p>If you need to get productive quickly, start with the core contract modules, then move outward into worktree and supervisor orchestration, then read the CLI dispatch tree last. The relationship diagram below intentionally stays coarse so it explains the system instead of fighting you.</p>
      <div class="two-up">
        <div class="callout">
          <h3>Recommended Read Order</h3>
          {_render_read_order(report["read_order"])}
        </div>
        <div class="diagram">
          {_render_package_relationship_svg(report)}
        </div>
      </div>
      <p class="caption">The CLI parses and routes commands, <span class="mono">blackdog_core</span> owns the durable backlog/runtime contract, and <span class="mono">blackdog</span> layers WTAM orchestration, rendering, bootstrap, and delegated execution on top.</p>
    </section>

    <section class="section">
      <h2>Canonical Workflows</h2>
      <p>The backlog is the canonical human-authored work graph. Everything else is either mutable state around that graph, append-only evidence about work happening against it, or rendered and supervisory projections built on top of those records.</p>
      <div class="subgrid">
        <div>
          <div class="workflow-grid">
            {_render_workflow_cards(report["workflows"])}
          </div>
        </div>
        <div class="diagram">
          {_render_backlog_control_svg(report)}
        </div>
      </div>
      <p class="caption">The control flow above shows the durable path: <span class="mono">backlog.md</span> defines work, <span class="mono">backlog-state.json</span> tracks mutable claim and approval state, append-only logs carry chronology and coordination, and task results plus rendered views project the current reality back to operators.</p>
    </section>

    <section class="section">
      <h2>Runtime Artifacts That Matter</h2>
      <p>The old field dump was low-signal. This section keeps the artifact inventory, but explains why each surface exists and which package layers actually touch it.</p>
      <div class="artifact-grid">
        {_render_artifact_cards(report["artifacts"])}
      </div>
    </section>

    <section class="section">
      <h2>Module And Class Guide</h2>
      <p>This is the internal API surface you read when modifying the system. Each module summary is derived from AST facts plus light package-specific hints; class summaries are built from class shape, fields, and methods so the page stays useful when terminology changes.</p>
      {_render_module_guide(report["module_guide"])}
    </section>

    <section class="section">
      <h2>CLI Command Inventory</h2>
      <p>The table below is walked from <span class="mono">build_parser()</span> at render time. It includes both grouping commands and leaf commands, the owning package from <span class="mono">COMMAND_SURFACES</span>, and the key flags or positional arguments a maintainer usually cares about first.</p>
      {_render_cli_inventory_table(report["cli_inventory"])}
    </section>

    <section class="section">
      <h2>Compatibility And Extensions</h2>
      <div class="subgrid">
        <div>
          {_render_compatibility_table(report["compatibility_modules"])}
        </div>
        <div>
          {_render_extension_table(report["extensions"])}
        </div>
      </div>
      <details>
        <summary>Raw Analyzer JSON</summary>
        <pre>{html_lib.escape(json.dumps(report, indent=2))}</pre>
      </details>
    </section>
  </main>
</body>
</html>
"""
    output_path.write_text(html, encoding="utf-8")
    return html


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _module_name(src_root: Path, path: Path) -> str:
    relative = path.relative_to(src_root).with_suffix("")
    parts = list(relative.parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _analyze_module(path: Path, *, module_name: str, src_root: Path, known_modules: frozenset[str]) -> ModuleFacts:
    source = path.read_text(encoding="utf-8")
    node = ast.parse(source, filename=str(path))
    is_package = path.name == "__init__.py"
    compatibility_shim = _is_compatibility_shim(node, source)
    imports = tuple(sorted(_internal_imports(node, module_name=module_name, is_package=is_package, known_modules=known_modules)))
    docstring = ast.get_docstring(node) or ""
    classes, class_facts, functions, dataclasses, artifact_reads, artifact_writes = _module_shapes(node)
    return ModuleFacts(
        name=module_name,
        path=str(path.relative_to(src_root.parent)),
        layer=_layer_for_module(module_name),
        is_package=is_package,
        compatibility_shim=compatibility_shim,
        docstring=docstring,
        imports=imports,
        classes=tuple(classes),
        class_facts=tuple(class_facts),
        functions=tuple(functions),
        dataclasses=tuple(dataclasses),
        artifact_reads=tuple(sorted(artifact_reads)),
        artifact_writes=tuple(sorted(artifact_writes)),
    )


def _module_shapes(node: ast.Module) -> tuple[list[str], list[ClassFacts], list[str], list[DataclassFacts], set[str], set[str]]:
    classes: list[str] = []
    class_facts: list[ClassFacts] = []
    functions: list[str] = []
    dataclasses: list[DataclassFacts] = []
    artifact_reads: set[str] = set()
    artifact_writes: set[str] = set()
    for child in node.body:
        if isinstance(child, ast.ClassDef):
            classes.append(child.name)
            is_dataclass = _is_dataclass(child)
            fields = tuple(_class_fields(child))
            if is_dataclass:
                dataclasses.append(DataclassFacts(name=child.name, fields=fields))
            class_facts.append(
                ClassFacts(
                    name=child.name,
                    bases=tuple(_dotted_name(base) for base in child.bases if _dotted_name(base)),
                    fields=fields,
                    methods=tuple(
                        grand.name
                        for grand in child.body
                        if isinstance(grand, (ast.FunctionDef, ast.AsyncFunctionDef)) and not grand.name.startswith("_")
                    ),
                    is_dataclass=is_dataclass,
                    docstring=ast.get_docstring(child) or "",
                )
            )
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(child.name)
            reads, writes = _function_artifacts(child)
            artifact_reads.update(reads)
            artifact_writes.update(writes)
    return classes, class_facts, functions, dataclasses, artifact_reads, artifact_writes


def _is_compatibility_shim(node: ast.Module, source: str) -> bool:
    docstring = ast.get_docstring(node) or ""
    if "Compatibility shim" in docstring:
        return True
    for child in ast.walk(node):
        if not isinstance(child, ast.Assign):
            continue
        for target in child.targets:
            if _is_sys_modules_name_target(target):
                return True
    return False


def _internal_imports(
    node: ast.Module,
    *,
    module_name: str,
    is_package: bool,
    known_modules: frozenset[str],
) -> set[str]:
    imports: set[str] = set()
    package_name = module_name if is_package else module_name.rsplit(".", 1)[0]
    for child in ast.walk(node):
        if isinstance(child, ast.Import):
            for alias in child.names:
                if alias.name in known_modules:
                    imports.add(alias.name)
        elif isinstance(child, ast.ImportFrom):
            if child.level:
                if not package_name:
                    continue
                base_expr = "." * child.level + (child.module or "")
                try:
                    base = importlib.util.resolve_name(base_expr, package_name)
                except ImportError:
                    continue
            else:
                base = child.module or ""
            if not any(base == package_name or base.startswith(f"{package_name}.") for package_name in PACKAGE_ROOTS):
                continue
            if base in known_modules:
                imports.add(base)
            for alias in child.names:
                if alias.name == "*":
                    continue
                candidate = f"{base}.{alias.name}" if base else alias.name
                if candidate in known_modules:
                    imports.add(candidate)
                elif base in known_modules:
                    imports.add(base)
    imports.discard(module_name)
    return imports


def _is_dataclass(node: ast.ClassDef) -> bool:
    for decorator in node.decorator_list:
        if _dotted_name(decorator).endswith("dataclass"):
            return True
    return False


def _class_fields(node: ast.ClassDef) -> list[str]:
    fields: list[str] = []
    for child in node.body:
        target: ast.expr | None = None
        if isinstance(child, ast.AnnAssign):
            target = child.target
        elif isinstance(child, ast.Assign) and len(child.targets) == 1:
            target = child.targets[0]
        if isinstance(target, ast.Name):
            fields.append(target.id)
    return fields


def _function_artifacts(node: ast.FunctionDef | ast.AsyncFunctionDef) -> tuple[set[str], set[str]]:
    visitor = _FunctionArtifactVisitor(function_name=node.name)
    visitor.visit(node)
    return visitor.reads, visitor.writes


class _FunctionArtifactVisitor(ast.NodeVisitor):
    def __init__(self, *, function_name: str) -> None:
        self.function_name = function_name
        self.fields: set[str] = set()
        self.reads: set[str] = set()
        self.writes: set[str] = set()
        self._read_hint = False
        self._write_hint = False

    def visit_Attribute(self, node: ast.Attribute) -> Any:
        field = _artifact_field(node)
        if field:
            self.fields.add(field)
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> Any:
        name = _terminal_name(node.func)
        if name in READ_HINTS:
            self._read_hint = True
        if name in WRITE_HINTS:
            self._write_hint = True
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> Any:
        if node.name == self.function_name:
            self.generic_visit(node)
            self._finalize()

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> Any:
        if node.name == self.function_name:
            self.generic_visit(node)
            self._finalize()

    def _finalize(self) -> None:
        if not self.fields:
            return
        if self._write_hint or self.function_name in WRITER_FUNCTIONS or self.function_name.startswith(WRITER_PREFIXES):
            self.writes.update(self.fields)
        if self._read_hint or (
            not self._write_hint and (self.function_name.startswith(READER_PREFIXES) or self.function_name not in WRITER_FUNCTIONS)
        ):
            self.reads.update(self.fields)
        if not self.reads and not self.writes:
            self.reads.update(self.fields)


def _artifact_field(node: ast.Attribute) -> str | None:
    chain = _attribute_chain(node)
    if len(chain) >= 2 and chain[-2] == "paths" and chain[-1] in ARTIFACT_FIELDS:
        return chain[-1]
    return None


def _attribute_chain(node: ast.AST) -> list[str]:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return list(reversed(parts))
    return []


def _terminal_name(node: ast.AST) -> str:
    if isinstance(node, ast.Attribute):
        return node.attr
    if isinstance(node, ast.Name):
        return node.id
    return ""


def _dotted_name(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        prefix = _dotted_name(node.value)
        return f"{prefix}.{node.attr}" if prefix else node.attr
    if isinstance(node, ast.Call):
        return _dotted_name(node.func)
    return ""


def _is_sys_modules_name_target(node: ast.AST) -> bool:
    if not isinstance(node, ast.Subscript):
        return False
    value_name = _dotted_name(node.value)
    if value_name != "sys.modules":
        return False
    slice_node = node.slice
    if isinstance(slice_node, ast.Name):
        return slice_node.id == "__name__"
    if isinstance(slice_node, ast.Constant):
        return slice_node.value == "__name__"
    return False


def _layer_for_module(module_name: str) -> str:
    if module_name == LAYER_CLI or module_name.startswith(f"{LAYER_CLI}."):
        return LAYER_CLI
    if module_name == LAYER_CORE or module_name.startswith(f"{LAYER_CORE}."):
        return LAYER_CORE
    return LAYER_PRODUCT


def _extract_command_surfaces(path: Path) -> dict[str, str]:
    node = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for child in node.body:
        if isinstance(child, ast.Assign):
            targets = child.targets
            value = child.value
        elif isinstance(child, ast.AnnAssign):
            targets = [child.target]
            value = child.value
        else:
            continue
        if value is None:
            continue
        for target in targets:
            if isinstance(target, ast.Name) and target.id == "COMMAND_SURFACES":
                payload = ast.literal_eval(value)
                return {
                    str(command): str(owner)
                    for command, owner in payload.items()
                    if isinstance(command, str) and isinstance(owner, str)
                }
    raise ArchitectureError(f"Could not find COMMAND_SURFACES in {path}")


def _commands_by_package(command_surfaces: dict[str, str]) -> dict[str, list[str]]:
    grouped: dict[str, list[str]] = defaultdict(list)
    for command, package_name in sorted(command_surfaces.items()):
        grouped[package_name].append(command)
    return {package_name: commands for package_name, commands in sorted(grouped.items())}


def _artifact_report(modules: list[ModuleFacts]) -> list[dict[str, Any]]:
    readers: dict[str, set[str]] = {field: set() for field in ARTIFACT_FIELDS}
    writers: dict[str, set[str]] = {field: set() for field in ARTIFACT_FIELDS}
    for module in modules:
        for field in module.artifact_reads:
            readers[field].add(module.name)
        for field in module.artifact_writes:
            writers[field].add(module.name)
    rows = []
    for field, metadata in ARTIFACT_FIELDS.items():
        rows.append(
            {
                "field": field,
                "label": metadata["label"],
                "surface": metadata["surface"],
                "readers": sorted(readers[field]),
                "writers": sorted(writers[field]),
            }
        )
    return rows


def _extension_report(project_root: Path) -> list[dict[str, Any]]:
    root = project_root / "extensions"
    if not root.is_dir():
        return []
    rows: list[dict[str, Any]] = []
    for entry in sorted(root.iterdir()):
        if not entry.is_dir():
            continue
        file_count = sum(1 for path in entry.rglob("*") if path.is_file())
        if file_count == 0:
            continue
        description = (
            "Editor extension surface that consumes documented CLI and artifact contracts."
            if entry.name == "emacs"
            else "Optional extension surface layered on top of Blackdog's documented contracts."
        )
        rows.append(
            {
                "name": f"extensions/{entry.name}",
                "surface": LAYER_EXTENSION,
                "files": file_count,
                "description": description,
            }
        )
    return rows


def _representative_modules(modules: list[ModuleFacts]) -> list[ModuleFacts]:
    module_index = {module.name: module for module in modules}
    selected: list[ModuleFacts] = []
    selected_names: set[str] = set()

    def add(module: ModuleFacts) -> None:
        if module.name in selected_names:
            return
        selected_names.add(module.name)
        selected.append(module)

    for name in PREFERRED_DIAGRAM_MODULES:
        module = module_index.get(name)
        if module is not None:
            add(module)

    for layer in LAYER_ORDER:
        rows = [module for module in modules if module.layer == layer and module.name not in selected_names]
        rows.sort(key=lambda module: (-len(module.imports), module.name))
        for module in rows[:2]:
            add(module)

    return selected[:12]


def _extract_cli_inventory(command_surfaces: dict[str, str]) -> list[dict[str, Any]]:
    try:
        cli_main = importlib.import_module("blackdog_cli.main")
    except ImportError as exc:
        raise ArchitectureError("Could not import blackdog_cli.main to inspect the argparse command tree") from exc
    parser_factory = getattr(cli_main, "build_parser", None)
    if not callable(parser_factory):
        raise ArchitectureError("blackdog_cli.main.build_parser() is not available")
    parser = parser_factory(description="Blackdog CLI")
    return _walk_cli_parser(parser, prefix="", command_surfaces=command_surfaces)


def _walk_cli_parser(
    parser: argparse.ArgumentParser,
    *,
    prefix: str,
    command_surfaces: dict[str, str],
) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for action in parser._actions:
        if not isinstance(action, argparse._SubParsersAction):
            continue
        help_by_name = {
            str(getattr(choice_action, "dest", "") or getattr(choice_action, "metavar", "") or ""): str(choice_action.help or "")
            for choice_action in getattr(action, "_choices_actions", [])
        }
        for name, subparser in sorted(action.choices.items()):
            command = f"{prefix} {name}".strip()
            has_children = any(isinstance(child, argparse._SubParsersAction) for child in subparser._actions)
            rows.append(
                {
                    "command": command,
                    "depth": len(command.split()),
                    "kind": "group" if has_children else "leaf",
                    "owner": command_surfaces.get(command, "unknown"),
                    "help": help_by_name.get(name, "").strip() or _first_sentence(subparser.description or ""),
                    "positionals": _parser_positionals(subparser),
                    "options": _parser_options(subparser),
                }
            )
            rows.extend(_walk_cli_parser(subparser, prefix=command, command_surfaces=command_surfaces))
    return rows


def _parser_positionals(parser: argparse.ArgumentParser) -> list[str]:
    rows: list[str] = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if action.option_strings:
            continue
        name = str(action.metavar or action.dest or "").strip()
        if not name or name == argparse.SUPPRESS:
            continue
        rows.append(name)
    return rows


def _parser_options(parser: argparse.ArgumentParser) -> list[str]:
    rows: list[str] = []
    for action in parser._actions:
        if isinstance(action, (argparse._HelpAction, argparse._SubParsersAction)):
            continue
        if not action.option_strings:
            continue
        names = [option for option in action.option_strings if option.startswith("--")] or list(action.option_strings)
        label = "/".join(names)
        if action.nargs != 0:
            metavar = action.metavar
            if isinstance(metavar, tuple):
                metavar = " ".join(str(part) for part in metavar)
            elif metavar is None:
                if action.choices and len(action.choices) <= 4:
                    metavar = "{" + "|".join(str(choice) for choice in action.choices) + "}"
                else:
                    metavar = str(action.dest or "").upper().replace("-", "_")
            label = f"{label} {metavar}".strip()
        rows.append(label)
    return rows


def _build_module_guide(module_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    sections: list[dict[str, Any]] = []
    for package_name in PACKAGE_ROOTS:
        rows = [
            _module_guide_row(module)
            for module in sorted(
                (module for module in module_rows if _package_for_module(module["name"]) == package_name),
                key=lambda module: _module_sort_key(module["name"]),
            )
        ]
        if not rows:
            continue
        sections.append(
            {
                "package": package_name,
                "summary": _package_summary(package_name),
                "modules": rows,
            }
        )
    return sections


def _workflow_report(cli_inventory: list[dict[str, Any]], artifacts: list[dict[str, Any]]) -> list[dict[str, Any]]:
    commands = {row["command"] for row in cli_inventory}
    artifact_labels = {row["field"]: row["label"] for row in artifacts}

    def available(*required: str) -> bool:
        return all(command in commands for command in required)

    rows: list[dict[str, Any]] = []
    if available("validate", "add", "summary", "next"):
        rows.append(
            {
                "title": "Shape And Prioritize Work",
                "summary": "This is the canonical backlog-authoring loop. Humans edit the work graph here, while the runtime state and event log only record approvals, comments, and selection around that graph.",
                "artifacts": [
                    artifact_labels["backlog_file"],
                    artifact_labels["state_file"],
                    artifact_labels["events_file"],
                ],
                "steps": [
                    {
                        "commands": ["validate"],
                        "detail": "Load the profile plus backlog/state/inbox/events contract before making or trusting any changes.",
                    },
                    {
                        "commands": ["add", "task edit"],
                        "detail": "Create or reshape task payloads, narrative, lanes, objectives, and task-shaping metadata inside backlog markdown.",
                    },
                    {
                        "commands": ["comment", "decide"],
                        "detail": "Attach human review, approval state, and project commentary without mutating the durable event history.",
                    },
                    {
                        "commands": ["summary", "plan", "next"],
                        "detail": "Project the current backlog plus reconciled state into queue views that select runnable work.",
                    },
                ],
            }
        )
    if available("claim", "worktree preflight", "worktree start", "result record", "worktree land", "complete"):
        rows.append(
            {
                "title": "Execute One Task Under WTAM",
                "summary": "Blackdog's manual-first implementation path keeps kept code edits inside a branch-backed task worktree, then records evidence before landing back through the primary checkout.",
                "artifacts": [
                    artifact_labels["backlog_file"],
                    artifact_labels["state_file"],
                    artifact_labels["events_file"],
                    artifact_labels["results_dir"],
                ],
                "steps": [
                    {
                        "commands": ["claim"],
                        "detail": "Reserve a runnable task in state so the backlog and event log know which agent currently owns the slice.",
                    },
                    {
                        "commands": ["worktree preflight", "worktree start"],
                        "detail": "Verify the primary checkout is safe, derive branch/worktree metadata, and move implementation into a task workspace.",
                    },
                    {
                        "commands": ["result record"],
                        "detail": "Write structured closeout evidence describing what changed, what was validated, and what still needs attention.",
                    },
                    {
                        "commands": ["worktree land", "complete"],
                        "detail": "Land the branch back through the primary worktree, then mark the task done in the state machine.",
                    },
                ],
            }
        )
    if available("supervise run", "supervise status", "supervise recover", "inbox send", "inbox resolve"):
        rows.append(
            {
                "title": "Delegate And Recover Work",
                "summary": "Supervisor runs layer child-agent orchestration on top of the same backlog, state, inbox, results, and worktree contracts instead of inventing a second control plane.",
                "artifacts": [
                    artifact_labels["inbox_file"],
                    artifact_labels["results_dir"],
                    artifact_labels["supervisor_runs_dir"],
                ],
                "steps": [
                    {
                        "commands": ["supervise run", "supervise sweep"],
                        "detail": "Select runnable work, prepare child workspaces, launch agents, and keep attempt metadata under supervisor run directories.",
                    },
                    {
                        "commands": ["inbox send", "inbox list", "inbox resolve"],
                        "detail": "Coordinate operator or child follow-up through append-only inbox messages and resolution records.",
                    },
                    {
                        "commands": ["supervise status", "supervise recover", "supervise report"],
                        "detail": "Project run health, recover partial or blocked executions, and summarize outcome history without bypassing the base runtime artifacts.",
                    },
                ],
            }
        )
    if available("create-project", "bootstrap", "refresh", "update-repo", "render", "architecture-docs"):
        rows.append(
            {
                "title": "Bootstrap And Maintain An Install",
                "summary": "Scaffold and host-maintenance commands create the repo contract, keep managed skills current, and regenerate human-facing HTML views that explain or render the runtime state.",
                "artifacts": [
                    artifact_labels["profile_file"],
                    artifact_labels["html_file"],
                    artifact_labels["threads_dir"],
                ],
                "steps": [
                    {
                        "commands": ["create-project", "bootstrap", "init"],
                        "detail": "Create or initialize repo-local Blackdog artifacts, profile defaults, and the initial control-root layout.",
                    },
                    {
                        "commands": ["refresh", "update-repo", "installs observe"],
                        "detail": "Refresh managed project files, update downstream installs, and observe the state of tracked hosts.",
                    },
                    {
                        "commands": ["render", "architecture-docs"],
                        "detail": "Regenerate human-facing HTML views: the live backlog board and this code-derived maintainer overview.",
                    },
                ],
            }
        )
    return rows


def _read_order_report(module_rows: list[dict[str, Any]]) -> list[dict[str, str]]:
    priorities = (
        "blackdog_core.profile",
        "blackdog_core.backlog",
        "blackdog_core.state",
        "blackdog.worktree",
        "blackdog.supervisor",
        "blackdog_cli.main",
        "blackdog.board",
        "blackdog.scaffold",
        "blackdog.architecture",
    )
    module_index = {row["name"]: row for row in module_rows}
    rows: list[dict[str, str]] = []
    for name in priorities:
        module = module_index.get(name)
        if module is None:
            continue
        rows.append(
            {
                "module": name,
                "summary": _module_summary(module),
                "read_hint": _module_read_hint(module),
            }
        )
    return rows


def _package_for_module(module_name: str) -> str:
    for package_name in PACKAGE_ROOTS:
        if module_name == package_name or module_name.startswith(f"{package_name}."):
            return package_name
    return module_name.split(".", 1)[0]


def _package_summary(package_name: str) -> str:
    summaries = {
        "blackdog_core": "Durable contract layer. Read this first to understand path resolution, backlog semantics, runtime snapshots, and the canonical claim/approval/result state machines.",
        "blackdog": "Product layer. This packages WTAM worktree flow, supervisor behavior, scaffolding, rendering, installs, tuning, and the other operator-facing behaviors built on top of the core contract.",
        "blackdog_cli": "Thin executable layer. Read this last when you need to understand how the public CLI surface parses arguments and dispatches into core or product modules.",
    }
    return summaries.get(package_name, f"Maintainer-facing package section for {package_name}.")


def _module_guide_row(module: dict[str, Any]) -> dict[str, Any]:
    public_functions = [name for name in module["functions"] if not name.startswith("_")]
    private_helper_count = len(module["functions"]) - len(public_functions)
    import_labels = _unique_preserving_order(_short_module(name) for name in module["imports"])
    artifact_labels = _unique_preserving_order(
        [ARTIFACT_FIELDS[field]["label"] for field in module["artifact_reads"]]
        + [ARTIFACT_FIELDS[field]["label"] for field in module["artifact_writes"]]
    )
    class_rows = [
        {
            "name": class_row["name"],
            "summary": _class_summary(class_row, module),
            "bases": list(class_row["bases"]),
            "fields": list(class_row["fields"]),
            "methods": list(class_row["methods"]),
            "kind": "dataclass" if class_row["is_dataclass"] else "class",
        }
        for class_row in module["class_facts"]
    ]
    return {
        "name": module["name"],
        "path": module["path"],
        "summary": _module_summary(module),
        "read_hint": _module_read_hint(module),
        "imports": import_labels,
        "public_functions": _display_public_functions(public_functions),
        "public_function_total": len(public_functions),
        "private_helper_count": private_helper_count,
        "classes": class_rows,
        "artifacts": artifact_labels,
        "compatibility_shim": bool(module["compatibility_shim"]),
    }


def _module_summary(module: dict[str, Any]) -> str:
    module_name = module["name"]
    if module_name in MODULE_SUMMARY_OVERRIDES:
        return MODULE_SUMMARY_OVERRIDES[module_name]
    docstring = _first_sentence(module.get("docstring", ""))
    if docstring:
        return docstring
    public_functions = [name for name in module["functions"] if not name.startswith("_")]
    if module["artifact_writes"]:
        targets = _human_join(ARTIFACT_FIELDS[field]["label"] for field in module["artifact_writes"][:3])
        return f"Owns write paths for {targets} and exposes {len(public_functions)} public entrypoints around that behavior."
    if module["artifact_reads"]:
        targets = _human_join(ARTIFACT_FIELDS[field]["label"] for field in module["artifact_reads"][:3])
        return f"Reads {targets} and exposes {len(public_functions)} public entrypoints for projection or validation."
    if public_functions:
        return f"Provides {len(public_functions)} public entrypoints for {module_name.rsplit('.', 1)[-1].replace('_', ' ')} behavior."
    return f"Internal support module for {module_name}."


def _module_read_hint(module: dict[str, Any]) -> str:
    return MODULE_READ_HINTS.get(module["name"], f"Read this when {module['name']} behavior changes or an adjacent command dispatches here.")


def _class_summary(class_row: dict[str, Any], module: dict[str, Any]) -> str:
    name = class_row["name"]
    fields = list(class_row["fields"])
    methods = list(class_row["methods"])
    field_set = set(fields)
    if name.endswith("Error"):
        return f"Exception type for {module['name']} failures. Raise or catch this when the module rejects input or cannot complete its contract safely."
    if field_set and all(field.endswith(("_file", "_dir", "_root")) or field in {"project_root"} for field in field_set):
        return "Dataclass carrying the resolved filesystem contract for one repo install. It names the canonical profile, backlog, state, event, result, thread, HTML, skill, and worktree paths other layers consume."
    if {"project_name", "profile_version", "paths"} <= field_set:
        return "Dataclass carrying repo identity, workflow policy defaults, and resolved paths. This is the configuration object most top-level flows pass around after loading `blackdog.toml`."
    if {"payload", "narrative"} <= field_set:
        return "Dataclass representing one parsed backlog task plus its narrative and plan placement. It is the main in-memory task object once markdown has been validated and loaded."
    if {"raw_text", "tasks", "plan"} <= field_set:
        return "Dataclass for the parsed backlog document. It keeps the original markdown, section structure, task list, and plan block together so edits can be rendered back safely."
    if {"backlog", "state", "events", "inbox", "results"} <= field_set:
        return "Dataclass bundling the live runtime artifacts Blackdog needs for summaries, rendering, and task selection. It is the bridge between on-disk artifacts and readonly snapshot views."
    if {"task", "child_agent", "workspace", "run_dir"} <= field_set:
        return "Dataclass tracking one delegated child execution attempt. It carries launch metadata, open process handles, landing state, result evidence, and telemetry for supervisor recovery and reporting."
    if {"workspace", "worktree_spec"} <= field_set:
        return "Dataclass representing a prepared execution workspace before or during supervisor launch. It lets workspace setup and run logic share a stable handoff object."
    if "branch" in field_set and "worktree_path" in field_set:
        return "Dataclass describing the WTAM branch-backed workspace for one task. It captures branch, base, target, and filesystem location so start, land, and cleanup flows can operate deterministically."
    if name.endswith("Narrative") and fields:
        return "Dataclass for the prose attached to a task. It keeps the why, required evidence, and affected paths adjacent to the raw task payload."
    if class_row["is_dataclass"]:
        detail = ""
        if fields:
            detail = f" It carries {_field_phrase(fields)}."
        elif methods:
            detail = f" Its public methods are {_human_join(methods)}."
        return f"Dataclass representing {_class_role_phrase(name)} in {module['name']}.{detail}"
    if methods:
        return f"Helper class with public methods {_human_join(methods)} used inside {module['name']}."
    bases = list(class_row["bases"])
    if bases:
        return f"Internal {name} type built on {_human_join(bases)} for {module['name']}."
    return f"Internal helper type inside {module['name']}."


def _class_role_phrase(name: str) -> str:
    words = name.replace("_", " ")
    words = re.sub(r"(?<!^)(?=[A-Z])", " ", words).lower()
    replacements = {
        "paths": "the resolved path contract",
        "profile": "repo configuration and policy",
        "task": "a task record",
        "snapshot": "a snapshot view",
        "artifacts": "a runtime artifact bundle",
        "run": "a delegated run record",
        "workspace": "a prepared workspace",
        "narrative": "task narrative metadata",
    }
    for suffix, phrase in replacements.items():
        if words.endswith(suffix):
            return phrase
    return words


def _field_phrase(fields: list[str]) -> str:
    if not fields:
        return "no durable fields"
    if len(fields) > 6:
        return _human_join(fields[:5]) + ", and related fields"
    return _human_join(fields)


def _display_public_functions(functions: list[str], *, limit: int = 18) -> list[str]:
    if len(functions) <= limit:
        return functions
    return [*functions[:limit], f"+{len(functions) - limit} more"]


def _unique_preserving_order(values: list[str] | tuple[str, ...] | Any) -> list[str]:
    seen: set[str] = set()
    rows: list[str] = []
    for raw in values:
        value = str(raw)
        if not value or value in seen:
            continue
        seen.add(value)
        rows.append(value)
    return rows


def _module_sort_key(module_name: str) -> tuple[int, str]:
    priorities = (
        "blackdog_core.profile",
        "blackdog_core.backlog",
        "blackdog_core.state",
        "blackdog_core.snapshot",
        "blackdog.worktree",
        "blackdog.supervisor",
        "blackdog.supervisor_policy",
        "blackdog.board",
        "blackdog.scaffold",
        "blackdog.conversations",
        "blackdog.installs",
        "blackdog.tuning",
        "blackdog.architecture",
        "blackdog_cli.main",
    )
    try:
        return (priorities.index(module_name), module_name)
    except ValueError:
        return (len(priorities), module_name)


def _first_sentence(text: str) -> str:
    normalized = " ".join(text.split())
    if not normalized:
        return ""
    match = re.search(r"(.+?[.!?])(?:\s|$)", normalized)
    return match.group(1).strip() if match else normalized


def _human_join(values: Any) -> str:
    rows = [str(value) for value in values if str(value)]
    if not rows:
        return ""
    if len(rows) == 1:
        return rows[0]
    if len(rows) == 2:
        return f"{rows[0]} and {rows[1]}"
    return f"{', '.join(rows[:-1])}, and {rows[-1]}"


def _overview_cards(summary: dict[str, Any]) -> str:
    cards = [
        ("Python modules", summary["module_count"]),
        ("Classes", summary["class_count"]),
        ("Dataclasses", summary["dataclass_count"]),
        ("Artifact surfaces", summary["artifact_surface_count"]),
        ("CLI commands", summary["command_count"]),
        ("Extensions", summary["extension_surface_count"]),
        ("Compatibility modules", summary["compatibility_module_count"]),
    ]
    return "".join(
        f'<div class="summary-card"><div class="summary-value">{value}</div><div class="summary-label">{html_lib.escape(label)}</div></div>'
        for label, value in cards
    )


def _render_read_order(rows: list[dict[str, str]]) -> str:
    body = ['<ol class="read-order">']
    for row in rows:
        body.append(
            '<li class="read-step">'
            f'<strong>{html_lib.escape(row["module"])}</strong>'
            f'<div>{html_lib.escape(row["summary"])}</div>'
            f'<div class="muted">{html_lib.escape(row["read_hint"])}</div>'
            "</li>"
        )
    body.append("</ol>")
    return "".join(body)


def _render_package_relationship_svg(report: dict[str, Any]) -> str:
    width = 1120
    height = 420
    nodes = [_svg_defs()]
    positions: dict[str, tuple[int, int, int, int]] = {}
    cards = [
        (
            "blackdog_cli",
            "<<entrypoint>>",
            ["argparse tree", f"{report['summary']['command_count']} routed commands", "dispatch into core/product"],
            (42, 56),
            ("#eff6ff", "#1d4ed8"),
        ),
        (
            "blackdog_core",
            "<<durable contract>>",
            ["profile, backlog, state, snapshot", "claim/approval/result semantics", "artifact readers and writers"],
            (390, 36),
            ("#ecfdf5", "#047857"),
        ),
        (
            "blackdog",
            "<<product layer>>",
            ["worktree, supervisor, board, scaffold", "threads, installs, tuning, docs", "operator-facing orchestration"],
            (742, 56),
            ("#fff7ed", "#c2410c"),
        ),
        (
            "Runtime Artifacts",
            "<<shared storage>>",
            ["blackdog.toml, backlog.md, backlog-state.json", "events.jsonl, inbox.jsonl, task-results/", "threads/, supervisor-runs/, rendered HTML"],
            (390, 238),
            ("#fffdf8", "#475569"),
        ),
    ]
    for title, subtitle, lines, (x, y), colors in cards:
        fill, stroke = colors
        box_height = _node_height(lines)
        positions[title] = (x, y, 288, box_height)
        nodes.append(_svg_node(x, y, 288, box_height, title=title, subtitle=subtitle, lines=lines, fill=fill, stroke=stroke))
    edges = [
        _curved_arrow_between(positions["blackdog_cli"], positions["blackdog_core"], label="dispatch", color="#1d4ed8"),
        _curved_arrow_between(positions["blackdog_cli"], positions["blackdog"], label="dispatch", color="#1d4ed8"),
        _curved_arrow_between(positions["blackdog_core"], positions["Runtime Artifacts"], label="own contract", color="#047857"),
        _curved_arrow_between(positions["blackdog"], positions["Runtime Artifacts"], label="project / orchestrate", color="#c2410c"),
    ]
    return _svg_canvas(width, height, "".join([*nodes, *edges]))


def _render_backlog_control_svg(report: dict[str, Any]) -> str:
    width = 560
    height = 600
    nodes = [_svg_defs()]
    flow = [
        ("backlog.md", ["canonical work graph", "task narrative + plan"], (44, 36), ("#fff8eb", "#c2410c")),
        ("backlog-state.json", ["mutable claim + approval state", "reconciled against backlog"], (44, 146), ("#eefbf4", "#047857")),
        ("events.jsonl / inbox.jsonl", ["append-only chronology", "comments + coordination"], (44, 256), ("#eef2ff", "#4338ca")),
        ("task-results/ / threads/", ["structured closeout evidence", "conversation-linked context"], (44, 376), ("#eff6ff", "#1d4ed8")),
        ("Rendered Views", ["backlog board", "maintainer docs", "supervisor reports"], (44, 496), ("#fffdf8", "#475569")),
    ]
    positions: list[tuple[int, int, int, int]] = []
    for title, lines, (x, y), colors in flow:
        fill, stroke = colors
        box_height = _node_height(lines)
        positions.append((x, y, 472, box_height))
        nodes.append(_svg_node(x, y, 472, box_height, title=title, subtitle="", lines=lines, fill=fill, stroke=stroke))
    edges: list[str] = []
    for index in range(len(positions) - 1):
        start = positions[index]
        end = positions[index + 1]
        x1 = start[0] + start[2] / 2
        y1 = start[1] + start[3]
        x2 = end[0] + end[2] / 2
        y2 = end[1]
        edges.append(_labeled_arrow(x1, y1, x2, y2, label="project / append / render"))
    return _svg_canvas(width, height, "".join([*edges, *nodes]))


def _render_workflow_cards(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        body.append(
            '<article class="workflow-card">'
            '<div class="eyebrow">Workflow</div>'
            f'<h3>{html_lib.escape(row["title"])}</h3>'
            f'<p>{html_lib.escape(row["summary"])}</p>'
            f'<div class="chips">{_chip_list(row["artifacts"])}</div>'
            '<div class="step-list">'
        )
        for step in row["steps"]:
            commands = "".join(f'<span class="step-command">{html_lib.escape(command)}</span>' for command in step["commands"])
            body.append(
                '<div class="step-row">'
                f'<div class="chips">{commands}</div>'
                f'<div style="margin-top:0.45rem;">{html_lib.escape(step["detail"])}</div>'
                "</div>"
            )
        body.append("</div></article>")
    return "".join(body)


def _render_artifact_cards(rows: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for row in rows:
        explanation = ARTIFACT_EXPLANATIONS.get(row["field"], row["surface"])
        body.append(
            '<article class="artifact-card">'
            '<div class="eyebrow">Artifact</div>'
            f'<h3 class="mono">{html_lib.escape(row["label"])}</h3>'
            f'<p>{html_lib.escape(explanation)}</p>'
            '<div class="module-subgrid">'
            '<div class="callout">'
            '<h3>Readers</h3>'
            f'<div class="chips">{_chip_list([_short_module(name) for name in row["readers"]])}</div>'
            '</div>'
            '<div class="callout">'
            '<h3>Writers</h3>'
            f'<div class="chips">{_chip_list([_short_module(name) for name in row["writers"]])}</div>'
            '</div>'
            '</div>'
            '</article>'
        )
    return "".join(body)


def _render_module_guide(sections: list[dict[str, Any]]) -> str:
    body: list[str] = []
    for section in sections:
        body.append(
            '<section class="module-section">'
            '<div class="module-header">'
            f'<h3>{html_lib.escape(section["package"])}</h3>'
            f'<div class="module-meta">{len(section["modules"])} modules</div>'
            '</div>'
            f'<p>{html_lib.escape(section["summary"])}</p>'
            '<div class="module-grid">'
        )
        for module in section["modules"]:
            stats = [
                f"{module['public_function_total']} public entrypoints",
                f"{len(module['classes'])} classes",
                f"{module['private_helper_count']} private helpers",
            ]
            if module["compatibility_shim"]:
                stats.append("compatibility shim")
            body.append(
                '<article class="module-card">'
                f'<div class="module-title">{html_lib.escape(module["name"])}</div>'
                f'<div class="module-meta mono">{html_lib.escape(module["path"])}</div>'
                f'<p style="margin-top:0.75rem;">{html_lib.escape(module["summary"])}</p>'
                f'<p class="muted">{html_lib.escape(module["read_hint"])}</p>'
                f'<div class="chips">{_chip_list(stats)}</div>'
                '<div class="module-subgrid">'
                '<div class="callout">'
                '<h3>Public Entry Points</h3>'
                f'<div class="chips">{_chip_list(module["public_functions"])}</div>'
                '</div>'
                '<div class="callout">'
                '<h3>Artifacts And Imports</h3>'
                f'<div class="chips">{_chip_list(module["artifacts"] + module["imports"])}</div>'
                '</div>'
                '</div>'
                '<div class="class-list">'
            )
            if module["classes"]:
                for class_row in module["classes"]:
                    detail_bits = []
                    if class_row["bases"]:
                        detail_bits.append(f"bases: {_human_join(class_row['bases'])}")
                    if class_row["fields"]:
                        detail_bits.append(f"fields: {_field_phrase(class_row['fields'])}")
                    if class_row["methods"]:
                        detail_bits.append(f"methods: {_human_join(class_row['methods'])}")
                    body.append(
                        '<div class="class-row">'
                        f'<div class="module-title">{html_lib.escape(class_row["name"])}</div>'
                        f'<div class="module-meta">{html_lib.escape(class_row["kind"])}</div>'
                        f'<div style="margin-top:0.25rem;">{html_lib.escape(class_row["summary"])}</div>'
                        f'<div class="muted" style="margin-top:0.35rem;">{html_lib.escape(_human_join(detail_bits)) or "No public fields or methods exposed at module scope."}</div>'
                        '</div>'
                    )
            else:
                body.append('<div class="class-row muted">No classes in this module; the surface is functions plus helpers.</div>')
            body.append("</div></article>")
        body.append("</div></section>")
    return "".join(body)


def _render_cli_inventory_table(rows: list[dict[str, Any]]) -> str:
    body = [
        '<div class="table-wrap"><table class="wide-table"><thead><tr>'
        '<th>Command</th><th>Kind</th><th>Owner</th><th>Purpose</th><th>Positionals</th><th>Options</th>'
        '</tr></thead><tbody>'
    ]
    for row in rows:
        indent = max(0, int(row["depth"]) - 1) * 1.25
        body.append(
            "<tr>"
            f'<td class="mono nowrap" style="padding-left:{0.55 + indent:.2f}rem;">{html_lib.escape(row["command"])}</td>'
            f'<td class="nowrap">{html_lib.escape(row["kind"])}</td>'
            f'<td class="mono nowrap">{html_lib.escape(row["owner"])}</td>'
            f'<td class="wrap-anywhere">{html_lib.escape(row["help"] or "No argparse help text provided.")}</td>'
            f'<td>{_chip_list(row["positionals"])}</td>'
            f'<td>{_chip_list(row["options"])}</td>'
            "</tr>"
        )
    body.append("</tbody></table></div>")
    return "".join(body)


def _render_compatibility_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="callout"><h3>Compatibility Modules</h3><p>No compatibility shim modules were detected in the current checkout.</p></div>'
    body = [
        '<div class="callout"><h3>Compatibility Modules</h3><div class="table-wrap"><table class="dense-table"><thead><tr>'
        '<th>Module</th><th>Likely Target</th></tr></thead><tbody>'
    ]
    for row in rows:
        body.append(
            "<tr>"
            f'<td class="mono nowrap">{html_lib.escape(row["module"])}</td>'
            f'<td class="mono nowrap">{html_lib.escape(row["target"])}</td>'
            "</tr>"
        )
    body.append("</tbody></table></div></div>")
    return "".join(body)


def _render_extension_table(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return '<div class="callout"><h3>Extensions</h3><p>No optional extension surfaces were detected in the current checkout.</p></div>'
    body = [
        '<div class="callout"><h3>Extensions</h3><div class="table-wrap"><table class="dense-table"><thead><tr>'
        '<th>Surface</th><th>Files</th><th>Description</th></tr></thead><tbody>'
    ]
    for row in rows:
        body.append(
            "<tr>"
            f'<td class="mono nowrap">{html_lib.escape(row["name"])}</td>'
            f'<td class="nowrap">{int(row["files"])}</td>'
            f'<td>{html_lib.escape(row["description"])}</td>'
            "</tr>"
        )
    body.append("</tbody></table></div></div>")
    return "".join(body)


def _summary_cards(summary: dict[str, Any]) -> str:
    return _overview_cards(summary)


def _layer_chips(layer_counts: dict[str, int]) -> str:
    chips = []
    for layer in LAYER_ORDER:
        chips.append(f'<span class="chip">{html_lib.escape(layer)}: {int(layer_counts.get(layer, 0))}</span>')
    return "".join(chips)


def _render_layer_svg(report: dict[str, Any]) -> str:
    modules = report["diagram_modules"]
    grouped = {layer: [module for module in modules if module["layer"] == layer] for layer in LAYER_ORDER}
    column_width = 320
    node_width = 252
    column_gap = 54
    left_pad = 34
    top_pad = 86
    node_gap = 24
    max_nodes = max(1, max(len(rows) for rows in grouped.values()))
    height = top_pad + max_nodes * 118 + 90
    width = left_pad * 2 + len(LAYER_ORDER) * column_width + (len(LAYER_ORDER) - 1) * column_gap
    x_positions = {layer: left_pad + index * (column_width + column_gap) for index, layer in enumerate(LAYER_ORDER)}
    positions: dict[str, tuple[int, int, int, int]] = {}
    nodes = [_svg_defs()]
    edges: list[str] = []

    for layer in LAYER_ORDER:
        fill, stroke = LAYER_COLORS[layer]
        x = x_positions[layer]
        nodes.append(_svg_text(x + node_width / 2, 34, layer, size=14, weight="700", fill=stroke, anchor="middle"))
        y = top_pad
        for module in grouped[layer]:
            detail_lines = [
                module["path"],
                f"{len(module['imports'])} internal imports",
                f"{len(module['functions'])} functions / {len(module['dataclasses'])} dataclasses",
            ]
            box_height = _node_height(detail_lines)
            positions[module["name"]] = (x, y, node_width, box_height)
            nodes.append(
                _svg_node(
                    x,
                    y,
                    node_width,
                    box_height,
                    title=module["name"],
                    subtitle=f"<<{layer}>>",
                    lines=detail_lines,
                    fill=fill,
                    stroke=stroke,
                )
            )
            y += box_height + node_gap

    for module in modules:
        start = positions.get(module["name"])
        if start is None:
            continue
        sx, sy, sw, sh = start
        for imported_name in module["imports"]:
            end = positions.get(imported_name)
            if end is None:
                continue
            ex, ey, ew, eh = end
            if sx < ex:
                x1 = sx + sw
                x2 = ex
            elif sx > ex:
                x1 = sx
                x2 = ex + ew
            else:
                x1 = sx + sw / 2
                x2 = ex + ew / 2
            y1 = sy + sh / 2
            y2 = ey + eh / 2
            path = f"M{x1:.1f},{y1:.1f} C{(x1 + x2) / 2:.1f},{y1:.1f} {(x1 + x2) / 2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
            edges.append(
                f'<path d="{path}" fill="none" stroke="#94a3b8" stroke-opacity="0.78" stroke-width="1.8" marker-end="url(#arrow)"/>'
            )
    return _svg_canvas(width, height, "".join([*nodes, *edges]))


def _render_artifact_svg(report: dict[str, Any]) -> str:
    width = 1120
    height = 540
    nodes = [_svg_defs()]
    core_data = _top_dataclass_names(report, layer=LAYER_CORE)
    product_data = _top_dataclass_names(report, layer=LAYER_PRODUCT)
    extension_label = next((row["name"] for row in report["extensions"]), "extensions/*")
    left_nodes = [
        (
            "BlackdogPaths / RepoProfile",
            "<<blackdog_core>>",
            ["src/blackdog_core/profile.py", "repo-local contract + resolved paths"],
            (24, 52),
            LAYER_COLORS[LAYER_CORE],
        ),
        (
            "Read Models / Snapshots",
            "<<blackdog_core>>",
            core_data or ["RuntimeArtifacts", "RuntimeSnapshot", "RuntimeSummary"],
            (24, 204),
            LAYER_COLORS[LAYER_CORE],
        ),
        (
            "State Machines",
            "<<blackdog_core>>",
            ["approval / claim / inbox replay", "append-only events + task results"],
            (24, 366),
            LAYER_COLORS[LAYER_CORE],
        ),
    ]
    middle_nodes = [
        (
            "Repo Contract",
            "<<shared inputs>>",
            ["blackdog.toml", "validation + routing defaults", "worktree base + skill paths"],
            (404, 34),
            ("#fff8eb", "#c2410c"),
        ),
        (
            "Runtime Artifacts",
            "<<shared runtime>>",
            ["backlog.md", "backlog-state.json", "events.jsonl / inbox.jsonl", "task-results/"],
            (404, 196),
            ("#eefbf4", "#047857"),
        ),
        (
            "Blackdog Product Artifacts",
            "<<product surfaces>>",
            ["threads/", "<project>-backlog.html", "supervisor-runs/"],
            (404, 382),
            ("#fff8eb", "#c2410c"),
        ),
    ]
    right_nodes = [
        (
            "Board / Scaffold / Docs",
            "<<blackdog>>",
            ["blackdog.board", "blackdog.scaffold", "blackdog.architecture"],
            (786, 68),
            ("#eff6ff", "#1d4ed8"),
        ),
        (
            "WTAM / Supervisor",
            "<<blackdog>>",
            product_data or ["WorktreeSpec", "supervisor state", "tracked installs + threads"],
            (786, 250),
            ("#eff6ff", "#1d4ed8"),
        ),
        (
            "Optional Extensions",
            "<<extensions>>",
            [extension_label, "consume CLI + artifacts", "without owning runtime writes"],
            (786, 404),
            ("#f5f3ff", "#6d28d9"),
        ),
    ]
    positions: dict[str, tuple[int, int, int, int]] = {}
    for title, subtitle, lines, (x, y), colors in (*left_nodes, *middle_nodes, *right_nodes):
        fill, stroke = colors
        box_height = _node_height(lines)
        positions[title] = (x, y, 290, box_height)
        nodes.append(
            _svg_node(
                x,
                y,
                290,
                box_height,
                title=title,
                subtitle=subtitle,
                lines=lines,
                fill=fill,
                stroke=stroke,
            )
        )
    edges = [
        _curved_arrow_between(positions["BlackdogPaths / RepoProfile"], positions["Repo Contract"], label="load + resolve", color="#047857"),
        _curved_arrow_between(positions["BlackdogPaths / RepoProfile"], positions["Runtime Artifacts"], label="locate paths", color="#047857"),
        _curved_arrow_between(positions["Read Models / Snapshots"], positions["Runtime Artifacts"], label="parse + reconcile", color="#047857"),
        _curved_arrow_between(positions["State Machines"], positions["Runtime Artifacts"], label="read / write", color="#047857"),
        _curved_arrow_between(positions["State Machines"], positions["Blackdog Product Artifacts"], label="threads + run metadata", color="#047857"),
        _curved_arrow_between(positions["Runtime Artifacts"], positions["Board / Scaffold / Docs"], label="snapshot inputs", color="#1d4ed8"),
        _curved_arrow_between(positions["Blackdog Product Artifacts"], positions["Board / Scaffold / Docs"], label="rendered views", color="#1d4ed8"),
        _curved_arrow_between(positions["Runtime Artifacts"], positions["WTAM / Supervisor"], label="task and result evidence", color="#1d4ed8"),
        _curved_arrow_between(positions["Blackdog Product Artifacts"], positions["WTAM / Supervisor"], label="workflow state", color="#1d4ed8"),
        _curved_arrow_between(positions["Board / Scaffold / Docs"], positions["Optional Extensions"], label="documented surface", color="#6d28d9"),
    ]
    return _svg_canvas(width, height, "".join([*nodes, *edges]))


def _render_event_svg(report: dict[str, Any]) -> str:
    commands_by_package = report["commands_by_package"]
    core_commands = len(commands_by_package.get(LAYER_CORE, []))
    product_commands = len(commands_by_package.get(LAYER_PRODUCT, []))
    columns = [
        ("Operator / Agent", "shape work, claim, land"),
        ("blackdog_cli", "parse args + dispatch"),
        ("blackdog_core", "load, reconcile, mutate"),
        ("Shared Artifacts", "backlog/state/events/inbox/results"),
        ("blackdog", "render, supervise, worktree orchestration"),
    ]
    width = 1180
    height = 460
    start_x = 36
    lane_width = 200
    gap = 22
    lane_boxes = []
    edges = [_svg_defs()]
    for index, (title, subtitle) in enumerate(columns):
        x = start_x + index * (lane_width + gap)
        lane_boxes.append(
            _svg_node(
                x,
                26,
                lane_width,
                82,
                title=title,
                subtitle="<<lane>>",
                lines=[subtitle],
                fill="#fffdf8",
                stroke="#475569",
            )
        )
        edges.append(
            f'<line x1="{x + lane_width / 2:.1f}" y1="118" x2="{x + lane_width / 2:.1f}" y2="410" stroke="#cbd5e1" stroke-dasharray="6 8"/>'
        )
    step_y = [154, 228, 302, 376]
    cards = [
        (0, step_y[0], "shape work", "manual-first backlog and WTAM flow"),
        (1, step_y[0], "command routing", f"{core_commands} blackdog_core / {product_commands} blackdog"),
        (2, step_y[1], "reconcile + mutate", "load_backlog / load_state / append_event"),
        (3, step_y[2], "append-only evidence", "events.jsonl + inbox.jsonl + task-results/"),
        (4, step_y[3], "projection", "board render / supervisor / worktree views"),
    ]
    nodes: list[str] = []
    for column, y, title, subtitle in cards:
        x = start_x + column * (lane_width + gap) + 18
        nodes.append(
            _svg_node(
                x,
                y,
                lane_width - 36,
                48,
                title=title,
                subtitle="",
                lines=[subtitle],
                fill="#f8fafc",
                stroke="#64748b",
                line_height=13,
            )
        )
    edge_specs = [
        ((0, step_y[0] + 24), (1, step_y[0] + 24), "invoke"),
        ((1, step_y[0] + 52), (2, step_y[1] + 24), "dispatch"),
        ((2, step_y[1] + 52), (3, step_y[2] + 24), "append"),
        ((3, step_y[2] + 52), (4, step_y[3] + 24), "replay"),
    ]
    for (src_col, src_y), (dst_col, dst_y), label in edge_specs:
        x1 = start_x + src_col * (lane_width + gap) + lane_width / 2
        x2 = start_x + dst_col * (lane_width + gap) + lane_width / 2
        edges.append(_labeled_arrow(x1, src_y, x2, dst_y, label=label))
    return _svg_canvas(width, height, "".join([*edges, *lane_boxes, *nodes]))


def _render_actor_svg(report: dict[str, Any]) -> str:
    width = 1180
    height = 420
    actors = [
        ("Operator", "<<actor>>", "shape / claim / complete"),
        ("Primary Worktree", "<<workspace>>", "main branch + landing gate"),
        ("Task Worktree", "<<workspace>>", "branch-backed implementation"),
        ("Shared Control Root", "<<artifact set>>", "backlog + state + events + inbox + results"),
        ("Supervisor / Child", "<<actor>>", "optional delegated execution"),
    ]
    x = 34
    actor_width = 188
    gap = 36
    nodes = [_svg_defs()]
    centers: dict[str, float] = {}
    for title, subtitle, line in actors:
        nodes.append(
            _svg_node(
                x,
                26,
                actor_width,
                78,
                title=title,
                subtitle=subtitle,
                lines=[line],
                fill="#fffdf8",
                stroke="#475569",
            )
        )
        centers[title] = x + actor_width / 2
        nodes.append(
            f'<line x1="{x + actor_width / 2:.1f}" y1="116" x2="{x + actor_width / 2:.1f}" y2="382" stroke="#cbd5e1" stroke-dasharray="6 8"/>'
        )
        x += actor_width + gap
    steps = [
        ("Operator", "Primary Worktree", 150, "blackdog add / claim"),
        ("Primary Worktree", "Task Worktree", 206, "worktree start"),
        ("Task Worktree", "Shared Control Root", 262, "result record"),
        ("Task Worktree", "Primary Worktree", 318, "worktree land + complete"),
        ("Supervisor / Child", "Shared Control Root", 356, "supervise / inbox"),
    ]
    edges = [_labeled_arrow(centers[src], y, centers[dst], y, label=label) for src, dst, y, label in steps]
    return _svg_canvas(width, height, "".join([*edges, *nodes]))


def _top_dataclass_names(report: dict[str, Any], *, layer: str, limit: int = 3) -> list[str]:
    names: list[str] = []
    seen: set[str] = set()
    for row in report["dataclasses"]:
        if row["layer"] != layer or row["name"] in seen:
            continue
        seen.add(row["name"])
        names.append(row["name"])
        if len(names) == limit:
            break
    return names


def _render_dataclass_table(rows: list[dict[str, Any]]) -> str:
    body = ["<table><thead><tr><th>Layer</th><th>Module</th><th>Dataclass</th><th>Fields</th></tr></thead><tbody>"]
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{html_lib.escape(row['layer'])}</td>"
            f"<td class=\"mono\">{html_lib.escape(row['module'])}</td>"
            f"<td class=\"mono\">{html_lib.escape(row['name'])}</td>"
            f"<td>{_chip_list(row['fields'])}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")
    return "".join(body)


def _render_module_table(rows: list[dict[str, Any]]) -> str:
    body = ["<table><thead><tr><th>Layer</th><th>Module</th><th>Imports</th><th>Shapes</th></tr></thead><tbody>"]
    for row in rows:
        shapes = [
            f"{len(row['functions'])} functions",
            f"{len(row['dataclasses'])} dataclasses",
            "compatibility shim" if row["compatibility_shim"] else "",
        ]
        body.append(
            "<tr>"
            f"<td>{html_lib.escape(row['layer'])}</td>"
            f"<td><div class=\"mono\">{html_lib.escape(row['name'])}</div><div class=\"mono\" style=\"font-size:0.78rem;color:#52606d;\">{html_lib.escape(row['path'])}</div></td>"
            f"<td>{_chip_list([_short_module(name) for name in row['imports']])}</td>"
            f"<td>{_chip_list([shape for shape in shapes if shape])}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")
    return "".join(body)


def _render_artifact_table(rows: list[dict[str, Any]]) -> str:
    body = ["<table><thead><tr><th>Artifact</th><th>Surface</th><th>Readers</th><th>Writers</th></tr></thead><tbody>"]
    for row in rows:
        body.append(
            "<tr>"
            f"<td class=\"mono\">{html_lib.escape(row['label'])}</td>"
            f"<td>{html_lib.escape(row['surface'])}</td>"
            f"<td>{_chip_list([_short_module(name) for name in row['readers']])}</td>"
            f"<td>{_chip_list([_short_module(name) for name in row['writers']])}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")
    return "".join(body)


def _render_command_table(rows: dict[str, list[str]]) -> str:
    body = ["<table><thead><tr><th>Owning Package</th><th>Commands</th></tr></thead><tbody>"]
    for package_name, commands in rows.items():
        body.append(
            "<tr>"
            f"<td>{html_lib.escape(package_name)}</td>"
            f"<td>{_chip_list(commands)}</td>"
            "</tr>"
        )
    body.append("</tbody></table>")
    return "".join(body)


def _render_side_table(compatibility_modules: list[dict[str, Any]], extensions: list[dict[str, Any]]) -> str:
    sections = ["<div class=\"callout\">", "<h3>Compatibility Modules</h3>"]
    if compatibility_modules:
        sections.append(
            "<table><thead><tr><th>Module</th><th>Likely Target</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td class=\"mono\">{html_lib.escape(row['module'])}</td>"
                f"<td class=\"mono\">{html_lib.escape(row['target'])}</td>"
                "</tr>"
                for row in compatibility_modules
            )
            + "</tbody></table>"
        )
    else:
        sections.append("<p>No compatibility modules were detected.</p>")
    sections.append("</div>")
    sections.append("<div class=\"callout\" style=\"margin-top:1rem;\">")
    sections.append("<h3>Extensions</h3>")
    if extensions:
        sections.append(
            "<table><thead><tr><th>Surface</th><th>Files</th><th>Description</th></tr></thead><tbody>"
            + "".join(
                "<tr>"
                f"<td class=\"mono\">{html_lib.escape(row['name'])}</td>"
                f"<td>{int(row['files'])}</td>"
                f"<td>{html_lib.escape(row['description'])}</td>"
                "</tr>"
                for row in extensions
            )
            + "</tbody></table>"
        )
    else:
        sections.append("<p>No optional extension surfaces were detected.</p>")
    sections.append("</div>")
    return "".join(sections)


def _svg_canvas(width: int, height: int, body: str) -> str:
    return f'<svg viewBox="0 0 {width} {height}" width="{width}" height="{height}" role="img" aria-label="Blackdog architecture diagram">{body}</svg>'


def _svg_defs() -> str:
    return """
<defs>
  <marker id="arrow" markerWidth="10" markerHeight="10" refX="7" refY="3" orient="auto" markerUnits="strokeWidth">
    <path d="M0,0 L0,6 L8,3 z" fill="#64748b"/>
  </marker>
</defs>
"""


def _svg_node(
    x: int | float,
    y: int | float,
    width: int | float,
    height: int | float,
    *,
    title: str,
    subtitle: str,
    lines: list[str],
    fill: str,
    stroke: str,
    line_height: int = 16,
) -> str:
    title_y = y + 22
    parts = [
        f'<rect x="{x:.1f}" y="{y:.1f}" width="{width:.1f}" height="{height:.1f}" rx="18" fill="{fill}" stroke="{stroke}" stroke-width="1.8"/>',
        _svg_text(x + 16, title_y, subtitle, size=11, fill=stroke),
        _svg_text(x + 16, title_y + 16, title, size=15, weight="700"),
    ]
    current_y = title_y + 36
    for line in lines:
        wrapped = _wrap_text(line, width=max(22, int((width - 32) / 7)))
        for row in wrapped:
            parts.append(_svg_text(x + 16, current_y, row, size=11.5, fill="#334155"))
            current_y += line_height
    return "".join(parts)


def _svg_text(
    x: int | float,
    y: int | float,
    text: str,
    *,
    size: float = 12,
    fill: str = "#0f172a",
    weight: str = "500",
    anchor: str = "start",
) -> str:
    return (
        f'<text x="{x:.1f}" y="{y:.1f}" fill="{fill}" font-size="{size}" '
        f'font-family="Avenir Next, Segoe UI, sans-serif" font-weight="{weight}" text-anchor="{anchor}">{html_lib.escape(text)}</text>'
    )


def _node_height(lines: list[str]) -> int:
    wrapped_line_count = sum(max(1, len(_wrap_text(line, width=36))) for line in lines)
    return 58 + wrapped_line_count * 16


def _wrap_text(text: str, *, width: int) -> list[str]:
    words = re.split(r"(\s+)", text.strip())
    if not words:
        return [""]
    lines: list[str] = []
    current = ""
    for chunk in words:
        if not chunk:
            continue
        candidate = f"{current}{chunk}"
        if current and len(candidate) > width:
            lines.append(current.strip())
            current = chunk.lstrip()
        else:
            current = candidate
    if current.strip():
        lines.append(current.strip())
    return lines or [text]


def _curved_arrow_between(
    start: tuple[int, int, int, int],
    end: tuple[int, int, int, int],
    *,
    label: str,
    color: str,
) -> str:
    x1 = start[0] + start[2]
    y1 = start[1] + start[3] / 2
    x2 = end[0]
    y2 = end[1] + end[3] / 2
    return _curved_arrow(x1, y1, x2, y2, label=label, color=color)


def _curved_arrow(
    x1: float,
    y1: float,
    x2: float,
    y2: float,
    *,
    label: str,
    color: str,
) -> str:
    control = max(40.0, abs(x2 - x1) * 0.45)
    path = f"M{x1:.1f},{y1:.1f} C{x1 + control:.1f},{y1:.1f} {x2 - control:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
    mid_x = (x1 + x2) / 2
    mid_y = (y1 + y2) / 2 - 8
    return (
        f'<path d="{path}" fill="none" stroke="{color}" stroke-width="1.8" stroke-opacity="0.82" marker-end="url(#arrow)"/>'
        + _svg_text(mid_x, mid_y, label, size=10.5, fill=color, anchor="middle")
    )


def _labeled_arrow(x1: float, y1: float, x2: float, y2: float, *, label: str) -> str:
    path = f"M{x1:.1f},{y1:.1f} C{(x1 + x2) / 2:.1f},{y1:.1f} {(x1 + x2) / 2:.1f},{y2:.1f} {x2:.1f},{y2:.1f}"
    return (
        f'<path d="{path}" fill="none" stroke="#64748b" stroke-width="1.9" marker-end="url(#arrow)"/>'
        + _svg_text((x1 + x2) / 2, min(y1, y2) - 10, label, size=10.5, fill="#334155", anchor="middle")
    )


def _chip_list(values: list[str]) -> str:
    if not values:
        return '<span style="color:#64748b;">none</span>'
    return "".join(f'<span class="chip">{html_lib.escape(value)}</span>' for value in values)


def _short_module(name: str) -> str:
    for prefix in ("blackdog_cli.", "blackdog_core.", "blackdog."):
        if name.startswith(prefix):
            return name[len(prefix) :]
    return name
