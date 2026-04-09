from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
import ast
import html as html_lib
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


class ArchitectureError(RuntimeError):
    pass


@dataclass(frozen=True)
class DataclassFacts:
    name: str
    fields: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "fields": list(self.fields)}


@dataclass(frozen=True)
class ModuleFacts:
    name: str
    path: str
    layer: str
    is_package: bool
    compatibility_shim: bool
    imports: tuple[str, ...]
    classes: tuple[str, ...]
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
            "imports": list(self.imports),
            "classes": list(self.classes),
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
    artifacts = _artifact_report(non_package_modules)
    extensions = _extension_report(root)
    layer_counts = Counter(module.layer for module in non_package_modules)
    summary = {
        "module_count": len(non_package_modules),
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
        "modules": [module.to_dict() for module in non_package_modules],
        "diagram_modules": [module.to_dict() for module in _representative_modules(non_package_modules)],
        "dataclasses": dataclass_rows,
        "artifacts": artifacts,
        "command_surfaces": command_surfaces,
        "commands_by_package": commands_by_package,
        "compatibility_modules": compatibility_modules,
        "extensions": extensions,
    }


def render_architecture_html(report: dict[str, Any], output_path: Path) -> str:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    title = "Blackdog Architecture Diagrams"
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
      max-width: 1320px;
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
      word-break: break-word;
    }}
    @media (max-width: 920px) {{
      .header-grid,
      .subgrid {{
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
          <div class="meta-label">Generated Architecture Companion</div>
          <h1>{html_lib.escape(title)}</h1>
          <p class="lede">Code-derived diagrams for the current <span class="mono">blackdog_cli</span>, <span class="mono">blackdog_core</span>, and <span class="mono">blackdog</span> packages. This page is generated from the checked-out Python sources with the stdlib <span class="mono">ast</span> module so the module map, data structures, event flow, and WTAM workflow stay tied to code.</p>
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
      <p>The analyzer counts Python modules, dataclasses, runtime artifact readers and writers, CLI command ownership, and optional extensions directly from the checked-out source tree.</p>
      <div class="summary-grid">
        {_summary_cards(summary)}
      </div>
      <div class="subgrid" style="margin-top: 1rem;">
        <div class="callout">
          <h3>Layer Counts</h3>
          <div class="chips">{_layer_chips(summary["layer_counts"])}</div>
        </div>
        <div class="callout">
          <h3>Refresh</h3>
          <pre>{html_lib.escape("\n".join(command_lines))}</pre>
        </div>
      </div>
    </section>

    <section class="section">
      <h2>Layered Module Map</h2>
      <div class="diagram">
        {_render_layer_svg(report)}
      </div>
      <p class="caption">Representative modules are grouped by the current package split. Edges are internal imports discovered in the Python AST.</p>
    </section>

    <section class="section">
      <h2>Runtime Artifacts And Data Structures</h2>
      <div class="diagram">
        {_render_artifact_svg(report)}
      </div>
      <p class="caption">The center column is the repo/runtime artifact surface observed from code. The table lists dataclasses that define the main path contracts, read models, snapshots, and worktree state carriers.</p>
      <div style="margin-top: 1rem;">
        {_render_dataclass_table(report["dataclasses"])}
      </div>
    </section>

    <section class="section">
      <h2>Event Handling Flow</h2>
      <div class="diagram">
        {_render_event_svg(report)}
      </div>
      <p class="caption">Command ownership comes from <span class="mono">blackdog_cli.main.COMMAND_SURFACES</span>. The flow shows how the CLI dispatches into core state transitions and Blackdog product consumers that render views or orchestrate WTAM work.</p>
    </section>

    <section class="section">
      <h2>Actor And Worktree Flow</h2>
      <div class="diagram">
        {_render_actor_svg(report)}
      </div>
      <p class="caption">This sequence summarizes the documented manual-first WTAM flow for Blackdog-on-Blackdog work: shape work, claim it, move into a branch-backed task worktree, record results, then land through the primary worktree.</p>
    </section>

    <section class="section">
      <h2>Reference Tables</h2>
      <div class="subgrid">
        <div>
          <h3>Modules</h3>
          {_render_module_table(report["modules"])}
        </div>
        <div>
          <h3>Artifacts</h3>
          {_render_artifact_table(report["artifacts"])}
        </div>
      </div>
      <div class="subgrid" style="margin-top: 1rem;">
        <div>
          <h3>CLI Command Ownership</h3>
          {_render_command_table(report["commands_by_package"])}
        </div>
        <div>
          <h3>Compatibility And Extensions</h3>
          {_render_side_table(report["compatibility_modules"], report["extensions"])}
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
    classes, functions, dataclasses, artifact_reads, artifact_writes = _module_shapes(node)
    return ModuleFacts(
        name=module_name,
        path=str(path.relative_to(src_root.parent)),
        layer=_layer_for_module(module_name),
        is_package=is_package,
        compatibility_shim=compatibility_shim,
        imports=imports,
        classes=tuple(classes),
        functions=tuple(functions),
        dataclasses=tuple(dataclasses),
        artifact_reads=tuple(sorted(artifact_reads)),
        artifact_writes=tuple(sorted(artifact_writes)),
    )


def _module_shapes(node: ast.Module) -> tuple[list[str], list[str], list[DataclassFacts], set[str], set[str]]:
    classes: list[str] = []
    functions: list[str] = []
    dataclasses: list[DataclassFacts] = []
    artifact_reads: set[str] = set()
    artifact_writes: set[str] = set()
    for child in node.body:
        if isinstance(child, ast.ClassDef):
            classes.append(child.name)
            if _is_dataclass(child):
                dataclasses.append(DataclassFacts(name=child.name, fields=tuple(_class_fields(child))))
        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
            functions.append(child.name)
            reads, writes = _function_artifacts(child)
            artifact_reads.update(reads)
            artifact_writes.update(writes)
    return classes, functions, dataclasses, artifact_reads, artifact_writes


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


def _summary_cards(summary: dict[str, Any]) -> str:
    cards = [
        ("Python modules", summary["module_count"]),
        ("Dataclasses", summary["dataclass_count"]),
        ("Compatibility modules", summary["compatibility_module_count"]),
        ("Artifact surfaces", summary["artifact_surface_count"]),
        ("CLI commands", summary["command_count"]),
        ("Extensions", summary["extension_surface_count"]),
    ]
    return "".join(
        f'<div class="summary-card"><div class="summary-value">{value}</div><div class="summary-label">{html_lib.escape(label)}</div></div>'
        for label, value in cards
    )


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
