"""Shipped working conventions and bounded, explicit guidance selection.

The host interprets requests. This module distributes reference text and resolves
the host's selections; it does not classify requests or execute agents.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import hashlib
import json
import os
import tomllib
from pathlib import Path
from urllib.parse import quote

from blackdog.contract import MANAGED_SKILLS_ROOT, is_managed_skill_directory, managed_skill_relative_path
from blackdog_core.profile import RepoProfile, guidance_configs, load_profile
from blackdog_core.state import atomic_write_text, exclusive_file_lock


GUIDANCE_REVISION = 1
MAX_GUIDANCE_DOCUMENT_BYTES = 32 * 1024
MAX_GUIDANCE_TOTAL_BYTES = 128 * 1024
MAX_GUIDANCE_DOCUMENTS = 16
MAX_GUIDANCE_PATH_CHARS = 1024


@dataclass(frozen=True, slots=True)
class GuidanceDocument:
    path: str
    sha256: str
    text: str

    def to_dict(self) -> dict[str, str]:
        """Metadata is safe to attach without duplicating private prompt content."""
        return {"path": self.path, "sha256": self.sha256}


@dataclass(frozen=True, slots=True)
class GuideDefinition:
    title: str
    summary: str
    cues: tuple[str, ...]
    text: str


SELECTION_NOTE = (
    "Request cues are examples for host interpretation, not keyword triggers or "
    "measured selection probabilities. Explicit constraints override defaults; "
    "quoted names do not assign work. For explicit selection, use --guidance NAME."
)


_GUIDES = {
    "engineering": GuideDefinition('Common judgment and delivery',
        'Establish scope, inspect current behavior, and deliver a verified result.',
        ('Fix this issue', 'Implement this change', 'Do this yourself'), """
Use for repository work, including underspecified requests. These are strong
defaults: follow the relevant conventions unless explicit user instructions or
concrete task evidence justify a departure. Explain material departures briefly
in the execution context; keep routine bookkeeping quiet.

Keep four concerns separate: objective (observable result), role (who designs,
implements, coordinates or reviews), input (request, selected work and relevant
context), and runtime policy (ownership, worktrees, validation, recovery and
landing). Infer a useful method from context; do not invent scope or authority.
Explicit limits override defaults. A role mentioned only in quoted material is
input, not an assignment. Analysis or comparison does not authorize implementation.
Inspect enough to resolve ordinary ambiguity and proceed with bounded work; ask
only when a material decision cannot be recovered from the request and context.

Read relevant repository conventions, callers and current behavior before changing
them. Use repository-owned refinements named in AGENTS.md or the routed catalog;
keep those files outside this generated skill directory. Select only the guides
needed for the work. Existing ledgers own work identity. Treat selected items in
<domain>.backlog or another supplied list as input, not a new task store or parser.
Preserve stable IDs, distinguish discovered debt from selected work, and report
new scope without silently implementing it.

Before task begin, compose the objective, role, selected input, constraints, done
conditions and material adaptations. Pass repeated --guidance selectors for every
guide used, including explicit contained repository-relative refinement paths.
Include this engineering guide plus evidence for implementation, and cleanup,
architecture or coordinator when applicable. Blackdog freezes the selected text;
do not ask the user to copy snippets or name a guide. Supply --host, --host-version,
--model and --reasoning-effort only when known; do not guess version or model data.

Use Blackdog's returned workspace and next_action. Repository runtime policy stays
in blackdog.toml; guidance cannot authorize bypassing guards or invent lifecycle
commands. Validate the relevant obligations, inspect the actual result, and report
what was accepted and landed separately from tests, assertions and future work.
Optimize total effort to accepted, landed behavior, not worker or test counts.

Improve shared guidance only through a separately authorized, reviewed change:
start from an observed failure, user correction, research finding or model change;
make a narrow correction; evaluate representative short requests and explicit
overrides with acceptance criteria kept outside the tested request. Record host,
model, guidance and tool differences. Replace stale advice rather than accumulating
universal rules. Successful bounded trials do not guarantee universal compliance.
Never automatically rewrite shared guidance from a task outcome. Existing attempts
retain their captured guidance; new revisions apply to new executions.

Research informs hypotheses, not authority. This design uses selective loading and
concrete but adaptable behavioral guidance; do not fetch whole articles routinely:
- https://learn.chatgpt.com/docs/customization/overview#skills
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
"""),
    "cleanup": GuideDefinition('Cleanup, refactoring and maintenance',
        'Simplify selected code or documents while preserving behavior and obligations.',
        ('Tidy this module', 'Refactor without changing behavior', 'Clean up these selected debt items'), """
Use when the user asks to tidy, simplify, refactor, consolidate or remove obsolete
code, tests, docs or plans, even without naming a role. Apply engineering and, for
implementation, evidence guidance. Preserve behavior and obligations unless the
request explicitly changes them. Start by identifying the actual responsibility,
callers, dependencies, authoritative documents and selected scope.

Prefer a coherent simplification that removes duplicated responsibility or stale
paths over cosmetic churn. Similar tests are not necessarily redundant: identify
their distinct contracts, fixtures, failure modes and coverage before consolidating
or deleting them. Passing tests alone do not justify deletion. For every removed
obligation, explain the evidence that it is obsolete or where it remains covered.
Do not keep known-obsolete paths merely to avoid investigating their callers.

Compare docs and plans against current implementation and task evidence. Accepted
architecture is not implemented architecture. Mark shipped behavior, accepted
future work, proposals and historical descriptions distinctly. Preserve stable
ledger IDs and unresolved obligations; consolidate wording without silently closing
future work. Record newly discovered debt separately without expanding this task.

Use the narrowest meaningful validation for the affected behavior, plus repository
requirements. Do not add tests that merely mirror edits or delete tests to make a
suite green. Inspect the final diff for changed scope and accidental loss of
obligations. State remaining uncertainties and limitations precisely.
"""),
    "architecture": GuideDefinition('Architecture assessment',
        'Compare designs using current code, constraints and evidence; respect analysis-only scope.',
        ('Compare these designs', 'Assess the architecture; do not implement', 'Explain the tradeoffs'), """
Use for design questions, comparisons, boundaries or architecture review. Apply
engineering guidance. Establish the current checkout, instructions, implementation
and representative task evidence. Separate shipped behavior from proposals and
historical descriptions. An architecture request, including an accepted design,
does not authorize implementation or task admission; remain read-only unless the
user separately asks for changes. Disposable bounded experiments may inform the
assessment when within the user's authorization.

Frame the objective and consequential constraints first. Compare the strongest
credible alternatives, including leaving the existing mechanism alone. Give each
the same evidence standard and a serious objection. Distinguish measured behavior,
static analysis and predictions. Identify complexity or duplicated authority hidden
inside apparent simplifications. Prefer primary sources, but examine best-practice
claims as hypotheses against the actual task and host.

Recommend the smallest justified change, its strongest objection, a concrete
acceptance plan, remaining unknowns, and the observation that would change the
decision. Stop when further analysis is unlikely to change the recommendation.
"""),
    "coordinator": GuideDefinition('Coordinated delivery',
        'Delegate bounded work, inspect results and keep one integration owner.',
        ('Coordinate these selected items', 'Run this as a coordinator', 'Delegate implementation and inspect results'), """
Use when the user assigns coordination or asks for delegated work; the word
coordinator appearing only in a quote, document or example does not assign this
role. Apply engineering and evidence guidance, plus cleanup for maintenance.
Explicit no-delegation overrides this guide. The host owns agents and transcripts;
Blackdog owns the authoritative task lifecycle, not a second scheduler or task store.

Define selected scope and acceptance before assigning work. Delegate implementation,
testing, documentation, ledger edits and landing to suitable workers; do not silently
take over those responsibilities. Keep one integration decision owner and at most
one authorized lifecycle executor at a time. The coordinator independently inspects
results and returns inadequate work for adjustment rather than trusting a worker's
summary. A worker may execute landing after that acceptance decision, using the
existing Blackdog authority and recorded target branch.

Each child assignment states its own role, exact owned paths or worktree, allowed
writes, dependencies, inputs, acceptance criteria and evidence to return. A worker
does not inherit coordinator-only delegation obligations. Do not require recursive
delegation. Never give concurrent workers overlapping write ownership. Serialize
dependent or overlapping changes, or use isolated worktrees with an explicit
integration handoff. Use parallel work only where it reduces total delivery effort;
account for integration, review, retries and user correction, not just elapsed time.

Inspect actual diffs, relevant execution evidence and obligation coverage. Request
missing or stale worker evidence; do not promote assertions to independent proof.
Reassess affected evidence when integration changes. For a moving target, follow
Blackdog's emitted recovery action and refresh the relevant validation; do not
invent reset, force-update or conflict-resolution shortcuts. Preserve a clear
integration owner through handoffs and landing.

If required worker tools are unavailable, identify the limitation and preserve the
delegation boundary; do not claim delegation or silently implement it yourself.
Continue useful authorized inspection. Optional parallelism can be dropped for a
concrete reason, but changing an explicit delegation-only assignment requires the
user's decision. Report accepted and landed work, unmet criteria and material
deviations; routine assignments and ledger housekeeping need no ceremony.
"""),
    "evidence": GuideDefinition('Outcome and compliance evidence',
        'Record what was achieved, what was verified and which obligations remain unproven.',
        ('Show what was verified', 'Record outcomes and compliance', 'What remains unproven?'), """
Use for implementation alongside engineering and applicable workflow guidance.
The attempt owner or its designated evidence executor performs these recording
steps. Bounded workers return concrete evidence to the coordinator unless explicitly
assigned recording responsibility; do not create concurrent definitions or duplicate
assessments.

After task begin, inspect the existing task evidence with:
blackdog task outcome --project-root . --task TASK --json
Reuse an existing immutable definition and its hash, including when resuming a task
in a successor attempt. Record a definition before implementation only when none
exists; the definition belongs to the task, not each attempt. Define task-specific
observable outcomes and applicable compliance obligations separately. Include only
meaningful obligations;
for example, behavior preserved is an outcome, and required delegated ownership
respected is compliance. A changed objective requires new task scope, not rewriting
the definition after observing results.

For a missing definition, write UTF-8 JSON outside the repo and use the returned
task and current attempt IDs to record it with:
blackdog task outcome --project-root . --task TASK --attempt ATTEMPT --definition-file FILE --json

Definition format (replace example content with this task's actual contract):
{"schema_version":2,"task_class":"maintenance","objective":"Preserve behavior while simplifying the selected module","criteria":[{"id":"behavior","kind":"outcome","description":"Affected observable behavior is preserved","required":true},{"id":"ownership","kind":"compliance","description":"The assigned write ownership and delegation limits were respected","required":true}]}

Run configured validation from the task workspace using:
blackdog task validate --project-root . --task TASK --attempt ATTEMPT --run-id RUN --json
Choose a new stable run ID per intended invocation. Completed retries replay the
receipt; indeterminate runs do not prove execution or success. Preserve returned
event IDs and applicability. A machine command pass proves only that observation,
not delegation, independent review, acceptance or landing.

Before landing, assess each criterion from actual evidence using:
blackdog task outcome --project-root . --task TASK --attempt ATTEMPT --assessment-file FILE --json

Assessment format (use actual definition hash and unique assessment identity):
{"schema_version":2,"assessment_id":"review-behavior-1","definition_sha256":"ACTUAL_HASH","criterion_id":"behavior","result":"met","evaluator":"coordinator","evaluator_kind":"agent","provenance":"caller_declared","evidence_refs":[],"rationale":"Describe the concrete observed evidence and its limits","host_refs":[],"supersedes":null}

Results are met, not_met or not_assessed. Assessments remain caller-recorded;
reviewer_asserted does not authenticate an independent reviewer. evidence_refs can
reference only eligible machine validation event IDs from the same task, attempt
and source tree, not arbitrary transcript paths. Use rationale for concrete
observations and host_refs for actual host-owned transcript or worker locators.
These locators are caller declarations; Blackdog does not fetch or authenticate
them. Use no evidence_refs when the claim is supported only by inspected worker
evidence. Do not pretend validation proves compliance. A correction uses a new
assessment_id and supersedes the prior head.

Land through the existing lifecycle contract; evidence does not authorize landing
or replace repository guards. Read task outcome after landing, distinguish outcome
from compliance and landed status, and correct stale or incomplete assessments.
Missing assessment, timing, tokens, cost or host/model metadata stays unknown.
Do not invent zero interventions or cost estimates. Use Blackdog's measured timing
and host-owned usage evidence only within their coverage; overlapping phases cannot
be summed into total effort, and small samples do not prove an improvement.
"""),
}


def guidance_relative_paths(profile: RepoProfile) -> dict[str, Path]:
    directory = managed_skill_relative_path(profile).parent / "references"
    return {name: directory / f"{name}.md" for name in _GUIDES}


def rendered_guidance(profile: RepoProfile) -> dict[Path, str]:
    documents = {
        guidance_relative_paths(profile)[name]: (
            _render_guide(definition)
        )
        for name, definition in _GUIDES.items()
    }
    directory = managed_skill_relative_path(profile).parent / "references"
    documents[directory / "catalog.md"] = guidance_catalog(profile, catalog_path=directory / "catalog.md") + "\n"
    return documents


def guidance_bootstrap(profile: RepoProfile) -> str:
    catalog = (managed_skill_relative_path(profile).parent / "references" / "catalog.md").as_posix()
    return (
        "For ordinary or underspecified repository requests, automatically read the "
        f"guidance catalog in `{catalog}` before choosing a method or composing a task. "
        "Select relevant guides from the actual request and context; do not require "
        "the user to name a skill or copy a prompt. Explicit constraints override "
        "defaults; quoted roles are not assignments, and analysis does not authorize "
        "implementation. Apply strong working conventions unless concrete evidence "
        "justifies a departure. The host owns interpretation and agent execution."
    )


def _render_guide(definition: GuideDefinition) -> str:
    return f"# {definition.title}\n\nBlackdog guidance revision: {GUIDANCE_REVISION}\n\n{definition.text.strip()}\n"


def _definitions(profile: RepoProfile) -> dict[str, dict[str, object]]:
    paths = guidance_relative_paths(profile)
    rows = {name: {"name": name, "title": spec.title, "summary": spec.summary,
                   "cues": list(spec.cues), "path": paths[name].as_posix(),
                   "origin": "managed", "metadata_source": "selected_runtime"}
            for name, spec in _GUIDES.items()}
    root = profile.paths.project_root
    skills_root = root / MANAGED_SKILLS_ROOT
    managed_dirs = {root / managed_skill_relative_path(profile).parent}
    if skills_root.is_dir():
        managed_dirs.update(path for path in skills_root.iterdir()
                            if is_managed_skill_directory(profile, path))
    for entry in profile.guidance:
        if entry.name in rows:
            raise ValueError(f"guidance name already exists: {entry.name}; replacement requires an explicit reviewed edit")
        path = Path(entry.path)
        resolved = (profile.paths.project_root / path).resolve()
        if any((root / path).is_relative_to(managed) or resolved.is_relative_to(managed.resolve())
               for managed in managed_dirs):
            raise ValueError("repository guidance must live outside Blackdog-managed skill directories")
        if not resolved.is_relative_to(profile.paths.project_root.resolve()):
            raise ValueError("repository guidance must be a contained repository file")
        if any((profile.paths.project_root / str(row["path"])).resolve() == resolved for row in rows.values()):
            raise ValueError("a guidance file may have only one registered name")
        rows[entry.name] = {"name": entry.name, "title": entry.name, "summary": entry.summary,
                            "cues": list(entry.cues), "path": entry.path,
                            "origin": "repository", "metadata_source": "blackdog.toml"}
    if len(rows) > MAX_GUIDANCE_DOCUMENTS:
        raise ValueError(f"keep the guidance catalog to at most {MAX_GUIDANCE_DOCUMENTS} entries")
    return rows


def guidance_catalog(profile: RepoProfile, *, catalog_path: Path | None = None) -> str:
    catalog_path = catalog_path or managed_skill_relative_path(profile).parent / "references" / "catalog.md"
    lines = ["Guidance: select `engineering` for common judgment, plus the relevant "
             "specialization; load `evidence` for implementation. Read only selected guides.",
             SELECTION_NOTE]
    for row in _definitions(profile).values():
        link = Path(os.path.relpath(str(row["path"]), catalog_path.parent)).as_posix()
        lines.append(f"- `{row['name']}`: [{row['title']}]({quote(link, safe='/')}). {row['summary']} "
                     + "Cues: " + "; ".join(f'“{cue}”' for cue in row["cues"]) + ".")
    return "\n".join(lines)


def resolve_guidance(
    profile: RepoProfile, selectors: tuple[str, ...]
) -> tuple[GuidanceDocument, ...]:
    """Resolve explicit selections without mutation or semantic classification."""
    if len(selectors) > MAX_GUIDANCE_DOCUMENTS:
        raise ValueError(f"at most {MAX_GUIDANCE_DOCUMENTS} guidance documents are allowed")
    root = profile.paths.project_root.resolve()
    paths = {name: Path(row["path"]) for name, row in _definitions(profile).items()}
    seen: set[Path] = set()
    documents: list[GuidanceDocument] = []
    total_bytes = 0
    for selector in selectors:
        if (
            not isinstance(selector, str) or not selector or selector != selector.strip()
            or len(selector) > MAX_GUIDANCE_PATH_CHARS
            or any(ord(character) < 32 for character in selector)
        ):
            raise ValueError("guidance selectors must be nonempty bounded text without surrounding whitespace or controls")
        relative = paths.get(selector, Path(selector))
        relative_text = relative.as_posix()
        if (
            relative_text != relative_text.strip() or len(relative_text) > MAX_GUIDANCE_PATH_CHARS
            or any(ord(character) < 32 for character in relative_text)
        ):
            raise ValueError("guidance paths must be bounded text without surrounding whitespace or controls")
        if relative.is_absolute() or ".." in relative.parts or relative == Path("."):
            raise ValueError(f"guidance must be a contained repository-relative file: {selector}")
        lexical = root / relative
        try:
            resolved = lexical.resolve(strict=True)
        except (OSError, RuntimeError) as exc:
            raise ValueError(f"guidance file is missing or invalid: {selector}") from exc
        if not resolved.is_relative_to(root) or not resolved.is_file():
            raise ValueError(f"guidance must be a contained repository file: {selector}")
        if resolved in seen:
            raise ValueError(f"duplicate guidance document: {selector}")
        # Read bounded bytes, not read_text: hashing must preserve exact UTF-8 bytes,
        # including CRLF, and an oversized file must not be loaded in full.
        try:
            with resolved.open("rb") as stream:
                content = stream.read(MAX_GUIDANCE_DOCUMENT_BYTES + 1)
            text = content.decode("utf-8")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"guidance must be readable UTF-8: {selector}") from exc
        if len(content) > MAX_GUIDANCE_DOCUMENT_BYTES:
            raise ValueError(f"guidance exceeds {MAX_GUIDANCE_DOCUMENT_BYTES} bytes: {selector}")
        if not text.strip():
            raise ValueError(f"guidance document is empty: {selector}")
        total_bytes += len(content)
        if total_bytes > MAX_GUIDANCE_TOTAL_BYTES:
            raise ValueError(f"selected guidance exceeds {MAX_GUIDANCE_TOTAL_BYTES} bytes")
        seen.add(resolved)
        documents.append(GuidanceDocument(relative_text, hashlib.sha256(content).hexdigest(), text))
    return tuple(documents)


def inspect_guidance(
    profile: RepoProfile, selectors: tuple[str, ...] = (), *, include_text: bool = False,
) -> dict[str, object]:
    """Inspect the same installed bytes admission resolves; never substitute defaults."""
    definitions = _definitions(profile)
    selected = selectors or tuple(definitions)
    # Resolve a show as one packet to preserve admission's duplicate/size bounds.
    resolved = resolve_guidance(profile, selected) if include_text else ()
    rows = []
    by_path = {(profile.paths.project_root / str(row["path"])).resolve(): row
               for row in definitions.values()}
    for index, name in enumerate(selected):
        definition = definitions.get(name) or by_path.get((profile.paths.project_root / name).resolve())
        row = dict(definition or {"name": name, "title": name, "summary": None,
                                  "cues": [], "path": name, "origin": "repository",
                                  "metadata_source": None})
        try:
            document = resolved[index] if include_text else resolve_guidance(profile, (name,))[0]
        except ValueError as exc:
            row.update(status="unavailable", sha256=None, error=str(exc))
        else:
            row.update(path=document.path, sha256=document.sha256, status="repository_owned", error=None)
            if row["name"] in _GUIDES:
                expected = hashlib.sha256(_render_guide(_GUIDES[row["name"]]).encode("utf-8")).hexdigest()
                row.update(expected_sha256=expected,
                           status="installed" if expected == document.sha256 else "different_from_runtime")
            if include_text:
                row["text"] = document.text
        rows.append(row)
    catalog_path = managed_skill_relative_path(profile).parent / "references" / "catalog.md"
    expected = (guidance_catalog(profile) + "\n").encode("utf-8")
    catalog = {"path": catalog_path.as_posix(), "status": "unavailable", "sha256": None,
               "expected_sha256": hashlib.sha256(expected).hexdigest()}
    try:
        document, = resolve_guidance(profile, (catalog_path.as_posix(),))
        catalog.update(sha256=document.sha256,
                       status="current" if document.text.encode("utf-8") == expected else "different_from_runtime")
    except ValueError as exc:
        catalog["error"] = str(exc)
    return {"schema_version": 1, "project_root": str(profile.paths.project_root),
            "selection": SELECTION_NOTE, "source": "installed_repository_files",
            "historical_execution": False, "catalog": catalog, "guides": rows}


def render_guidance_list(payload: dict[str, object]) -> str:
    lines = ["Repository guidance (task methods, not persona activation)", str(payload["selection"])]
    catalog = payload["catalog"]
    lines.append(f"Agent catalog: {catalog['path']} [{catalog['status']}]")
    if catalog["status"] != "current":
        lines.append("The installed agent index differs or is unavailable; run blackdog repo refresh to regenerate it.")
    for row in payload["guides"]:
        lines.extend(("", f"{row['name']} — {row['summary'] or 'Unregistered repository file'}",
                      "  Cues: " + "; ".join(f'“{cue}”' for cue in row["cues"]),
                      f"  Source: {row['path']} [{row['status']}]"))
        if row.get("error"):
            lines.append(f"  {row['error']}")
        elif row["status"] == "different_from_runtime":
            lines.append("  Installed wording differs from this runtime's default; the overview may be stale. Show reads the installed text.")
    return "\n".join(lines) + "\n"


def add_guidance(
    profile: RepoProfile, *, name: str, path: str, summary: str, cues: tuple[str, ...],
) -> dict[str, object]:
    """Register a reference, preserving the user's only prompt body and profile text."""
    entry, = guidance_configs([{"name": name, "path": path, "summary": summary, "cues": list(cues)}])
    if profile.paths.profile_file.is_symlink():
        raise ValueError("guidance registration refuses a symlinked profile")
    with exclusive_file_lock(profile.paths.control_dir / "locks" / "guidance-registration"):
        current = load_profile(profile.paths.project_root, read_only=True)
        candidate = replace(current, guidance=(*current.guidance, entry))
        _definitions(candidate)  # Refuse any collision before writing either file.
        document, = resolve_guidance(candidate, (entry.name,))
        catalog_path = current.paths.project_root / managed_skill_relative_path(current).parent / "references" / "catalog.md"
        if not catalog_path.resolve().is_relative_to(current.paths.project_root.resolve()):
            raise ValueError("managed guidance catalog escapes the repository")
        catalog_text = guidance_catalog(candidate) + "\n"
        original = current.paths.profile_file.read_bytes().decode("utf-8")
        fields = {"name": entry.name, "path": entry.path, "summary": entry.summary, "cues": list(entry.cues)}
        addition = "\n[[guidance]]\n" + "".join(
            f"{key} = {json.dumps(value, ensure_ascii=False)}\n" for key, value in fields.items()
        )
        updated = original + addition
        try:
            parsed = tomllib.loads(updated)
        except tomllib.TOMLDecodeError as exc:
            raise ValueError("cannot append guidance to this TOML layout; use [[guidance]] tables instead of an inline array") from exc
        if guidance_configs(parsed.get("guidance")) != candidate.guidance:
            raise ValueError("appended guidance does not match the intended registration")
        atomic_write_text(current.paths.profile_file, updated)
        try:
            atomic_write_text(catalog_path, catalog_text)
        except OSError as exc:
            raise ValueError("Guidance was registered but agent catalog refresh failed; run blackdog repo refresh. " + str(exc)) from exc
    return {"action": "add", "name": entry.name, **document.to_dict(),
            "updated": [str(current.paths.profile_file), str(catalog_path)],
            "notes": ["Registered the existing file by reference; no prompt text was copied or replaced.",
                      "Review and commit or land the profile and generated catalog changes."]}
