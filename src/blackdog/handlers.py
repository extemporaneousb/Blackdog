from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any
import os
import shlex
import subprocess
import sys
import time
import tomllib

from blackdog_core.profile import (
    BlackdogRuntimeHandlerConfig,
    HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE,
    HANDLER_INSTALL_MODE_LAUNCHER_SHIM,
    HANDLER_KIND_BLACKDOG_RUNTIME,
    HANDLER_KIND_PYTHON_OVERLAY_VENV,
    HANDLER_SCRIPT_POLICY_ROOT_BIN_FALLBACK,
    HANDLER_SOURCE_MODE_LOCAL_OVERRIDE,
    HANDLER_SOURCE_MODE_MANAGED_CHECKOUT,
    HANDLER_SOURCE_MODE_TARGET_REPO,
    PythonOverlayVenvHandlerConfig,
    RepoHandlerConfig,
    RepoProfile,
    resolve_config_path,
)


DEFAULT_SOURCE_REMOTE = "https://github.com/extemporaneousb/Blackdog.git"
DEFAULT_SOURCE_BRANCH = "main"
HANDLER_STATUS_BLOCKED = "blocked"
HANDLER_STATUS_CREATED = "created"
HANDLER_STATUS_PLANNED = "planned"
HANDLER_STATUS_PRESERVED = "preserved"
HANDLER_STATUS_UPDATED = "updated"
HANDLER_STATUS_VALIDATED = "validated"
_ROOT_BIN_FALLBACK_EXCLUDES = {
    "activate",
    "activate.csh",
    "activate.fish",
    "Activate.ps1",
    "blackdog",
    "pip",
    "pip3",
    "python",
    "python3",
}
_WORKTREE_EDITABLE_OVERLAY = "blackdog-worktree-editables.pth"


class HandlerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class HandlerAction:
    handler_id: str
    kind: str
    action: str
    target_path: str | None
    status: str
    message: str
    elapsed_ms: int | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.handler_id,
            "kind": self.kind,
            "action": self.action,
            "target_path": self.target_path,
            "status": self.status,
            "message": self.message,
            "elapsed_ms": self.elapsed_ms,
        }


@dataclass(frozen=True, slots=True)
class HandlerPlanSummary:
    ready: bool
    actions: tuple[HandlerAction, ...]
    remediation: str | None = None
    root_ve_path: str | None = None
    root_python_path: str | None = None
    worktree_ve_path: str | None = None
    worktree_python_path: str | None = None
    blackdog_path: str | None = None
    source_root: str | None = None
    source_mode: str | None = None
    runtime_mode: str | None = None
    script_policy: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ready": self.ready,
            "remediation": self.remediation,
            "root_ve_path": self.root_ve_path,
            "root_python_path": self.root_python_path,
            "worktree_ve_path": self.worktree_ve_path,
            "worktree_python_path": self.worktree_python_path,
            "blackdog_path": self.blackdog_path,
            "source_root": self.source_root,
            "source_mode": self.source_mode,
            "runtime_mode": self.runtime_mode,
            "script_policy": self.script_policy,
            "actions": [action.to_dict() for action in self.actions],
        }


@dataclass(frozen=True, slots=True)
class _PythonOverlayState:
    root_ve_path: Path
    root_python_path: Path
    root_bin_path: Path
    root_site_packages: Path
    worktree_ve_path: Path | None
    worktree_python_path: Path | None
    worktree_bin_path: Path | None
    worktree_site_packages: Path | None
    script_policy: str


@dataclass(frozen=True, slots=True)
class _BlackdogRuntimeState:
    blackdog_path: Path
    source_root: Path | None
    source_mode: str | None
    runtime_mode: str | None


@dataclass(frozen=True, slots=True)
class _HandlerContext:
    profile: RepoProfile
    operation: str
    project_root: Path
    worktree_path: Path | None = None
    source_root_override: Path | None = None
    update_managed_source: bool = False


@dataclass(frozen=True, slots=True)
class _HandlerStepResult:
    ready: bool
    actions: tuple[HandlerAction, ...]
    state: object | None
    remediation: str | None = None


def _run_command(
    *args: str,
    cwd: Path | None = None,
    env: dict[str, str] | None = None,
) -> str:
    completed = subprocess.run(
        list(args),
        cwd=str(cwd) if cwd is not None else None,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip() or f"exit code {completed.returncode}"
        raise HandlerError(f"{' '.join(args)} failed: {detail}")
    return completed.stdout.strip()


def _run_git(root: Path, *args: str) -> str:
    return _run_command("git", "-C", str(root), *args)


def _looks_like_blackdog_source_checkout(root: Path) -> bool:
    pyproject = root / "pyproject.toml"
    if not pyproject.is_file():
        return False
    if not (root / "src" / "blackdog_cli" / "main.py").is_file():
        return False
    try:
        payload = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except (OSError, tomllib.TOMLDecodeError):
        return False
    return str((payload.get("project") or {}).get("name") or "") == "blackdog"


def _current_blackdog_source_root() -> Path | None:
    candidate = Path(__file__).resolve().parents[2]
    if _looks_like_blackdog_source_checkout(candidate):
        return candidate
    return None


def _git_remote_url(repo_root: Path) -> str | None:
    try:
        remote = _run_git(repo_root, "remote", "get-url", "origin")
    except HandlerError:
        return None
    return remote or None


def _default_source_remote() -> str:
    current = _current_blackdog_source_root()
    if current is not None:
        remote = _git_remote_url(current)
        if remote:
            return remote
    return DEFAULT_SOURCE_REMOTE


def _resolve_handler_path(project_root: Path, base_root: Path, value: str) -> Path:
    if value.startswith("@git-common"):
        return resolve_config_path(project_root, value)
    candidate = Path(value)
    if candidate.is_absolute():
        return candidate
    return base_root / candidate


def _ordered_handlers(profile: RepoProfile) -> tuple[RepoHandlerConfig, ...]:
    lookup = {handler.handler_id: handler for handler in profile.handlers if handler.enabled}
    ordered: list[RepoHandlerConfig] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(handler_id: str) -> None:
        if handler_id in visited:
            return
        if handler_id in visiting:
            raise HandlerError(f"handler dependency cycle detected at {handler_id!r}")
        visiting.add(handler_id)
        handler = lookup[handler_id]
        for dependency in handler.depends_on:
            if dependency in lookup:
                visit(dependency)
        visiting.remove(handler_id)
        visited.add(handler_id)
        ordered.append(handler)

    for handler in profile.handlers:
        if handler.enabled:
            visit(handler.handler_id)
    return tuple(ordered)


def _python_version_tag(python_path: Path) -> str:
    return _run_command(
        str(python_path),
        "-c",
        "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')",
    ).strip()


def _site_packages_path(ve_path: Path, *, python_path: Path) -> Path:
    version_tag = _python_version_tag(python_path)
    return ve_path / "lib" / f"python{version_tag}" / "site-packages"


def _write_text_if_changed(path: Path, text: str, *, executable: bool = False) -> str:
    previous = path.read_text(encoding="utf-8") if path.exists() else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    if executable:
        path.chmod(0o755)
    if previous is None:
        return HANDLER_STATUS_CREATED
    if previous != text:
        return HANDLER_STATUS_UPDATED
    return HANDLER_STATUS_PRESERVED


def _map_path_from_root_to_worktree(path: Path, *, project_root: Path, worktree_path: Path) -> Path:
    resolved = path.expanduser().resolve()
    try:
        relative = resolved.relative_to(project_root)
    except ValueError:
        return resolved
    return (worktree_path / relative).resolve()


def _editable_source_paths(
    root_site_packages: Path,
    *,
    project_root: Path,
    worktree_path: Path,
    require_mapped_exists: bool,
) -> tuple[Path, ...]:
    if not root_site_packages.is_dir():
        return ()
    paths: list[Path] = []
    seen: set[Path] = set()
    for pth_file in sorted(root_site_packages.glob("*.pth")):
        if pth_file.name.startswith("blackdog-"):
            continue
        for raw_line in pth_file.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#") or line.startswith("import "):
                continue
            root_candidate = Path(line)
            if not root_candidate.expanduser().exists():
                continue
            candidate = _map_path_from_root_to_worktree(
                root_candidate,
                project_root=project_root,
                worktree_path=worktree_path,
            )
            if require_mapped_exists and not candidate.exists():
                continue
            if candidate in seen:
                continue
            seen.add(candidate)
            paths.append(candidate)
    return tuple(paths)


def _worktree_editable_action(
    config: PythonOverlayVenvHandlerConfig,
    state: _PythonOverlayState,
    *,
    project_root: Path,
    worktree_path: Path,
    execute: bool,
) -> HandlerAction | None:
    if state.worktree_site_packages is None:
        raise HandlerError("missing worktree site-packages path")
    source_paths = _editable_source_paths(
        state.root_site_packages,
        project_root=project_root,
        worktree_path=worktree_path,
        require_mapped_exists=execute,
    )
    if not source_paths:
        return None
    overlay_path = state.worktree_site_packages / _WORKTREE_EDITABLE_OVERLAY
    if execute:
        started = time.perf_counter()
        text = "".join(f"{path}\n" for path in source_paths)
        overlay_status = _write_text_if_changed(overlay_path, text)
        elapsed_ms = int((time.perf_counter() - started) * 1000)
    else:
        overlay_status = HANDLER_STATUS_PLANNED
        elapsed_ms = None
    return HandlerAction(
        handler_id=config.handler_id,
        kind=config.kind,
        action="wire-worktree-editables",
        target_path=str(overlay_path),
        status=overlay_status,
        message=f"wired {len(source_paths)} editable source path(s) into the worktree env",
        elapsed_ms=elapsed_ms,
    )


def _timed_action(
    *,
    handler_id: str,
    kind: str,
    action: str,
    target_path: Path | None,
    message: str,
    callback,
) -> HandlerAction:
    started = time.perf_counter()
    callback()
    elapsed_ms = int((time.perf_counter() - started) * 1000)
    return HandlerAction(
        handler_id=handler_id,
        kind=kind,
        action=action,
        target_path=str(target_path) if target_path is not None else None,
        status=HANDLER_STATUS_VALIDATED,
        message=message,
        elapsed_ms=elapsed_ms,
    )


def _symlink_root_bin_fallback(root_bin: Path, worktree_bin: Path) -> tuple[int, int]:
    created = 0
    preserved = 0
    worktree_bin.mkdir(parents=True, exist_ok=True)
    for item in sorted(root_bin.iterdir()):
        if item.name in _ROOT_BIN_FALLBACK_EXCLUDES:
            continue
        if not item.is_file() and not item.is_symlink():
            continue
        candidate = worktree_bin / item.name
        if candidate.exists():
            preserved += 1
            continue
        os.symlink(item, candidate)
        created += 1
    return created, preserved


def _ensure_managed_source_checkout(
    context: _HandlerContext,
    config: BlackdogRuntimeHandlerConfig,
) -> tuple[Path | None, str | None, tuple[HandlerAction, ...], str | None]:
    if _looks_like_blackdog_source_checkout(context.project_root):
        source_root = context.worktree_path or context.project_root
        action = HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="resolve-source",
            target_path=str(source_root),
            status=HANDLER_STATUS_VALIDATED,
            message="using the target repo checkout as the Blackdog source",
        )
        return source_root, HANDLER_SOURCE_MODE_TARGET_REPO, (action,), None

    if context.source_root_override is not None:
        if not _looks_like_blackdog_source_checkout(context.source_root_override):
            raise HandlerError(f"expected a Blackdog source checkout at {context.source_root_override}")
        actions: list[HandlerAction] = [HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="resolve-source",
            target_path=str(context.source_root_override),
            status=HANDLER_STATUS_VALIDATED,
            message="using local source override",
        )]
        if config.source_mode == HANDLER_SOURCE_MODE_MANAGED_CHECKOUT and context.operation in {"repo-install", "repo-update"}:
            managed_root = _resolve_handler_path(context.project_root, context.project_root, config.managed_source_dir)
            override_branch = _run_git(context.source_root_override, "rev-parse", "--abbrev-ref", "HEAD") or DEFAULT_SOURCE_BRANCH
            if (managed_root / ".git").is_dir():
                if context.update_managed_source:
                    started = time.perf_counter()
                    _run_command("git", "-C", str(managed_root), "pull", "--ff-only", str(context.source_root_override), override_branch)
                    actions.append(
                        HandlerAction(
                            handler_id=config.handler_id,
                            kind=config.kind,
                            action="update-managed-source",
                            target_path=str(managed_root),
                            status=HANDLER_STATUS_UPDATED,
                            message="updated the managed Blackdog source checkout from the local override",
                            elapsed_ms=int((time.perf_counter() - started) * 1000),
                        )
                    )
            elif not managed_root.exists():
                managed_root.parent.mkdir(parents=True, exist_ok=True)
                started = time.perf_counter()
                _run_command("git", "clone", str(context.source_root_override), str(managed_root))
                actions.append(
                    HandlerAction(
                        handler_id=config.handler_id,
                        kind=config.kind,
                        action="seed-managed-source",
                        target_path=str(managed_root),
                        status=HANDLER_STATUS_CREATED,
                        message="seeded the managed Blackdog source checkout from the local override",
                        elapsed_ms=int((time.perf_counter() - started) * 1000),
                    )
                )
        return context.source_root_override, HANDLER_SOURCE_MODE_LOCAL_OVERRIDE, tuple(actions), None

    if config.source_mode == HANDLER_SOURCE_MODE_TARGET_REPO:
        return None, None, (
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="resolve-source",
                target_path=str(context.project_root),
                status=HANDLER_STATUS_BLOCKED,
                message="target-repo source mode requires the target repo to be a Blackdog checkout",
            ),
        ), "run `blackdog repo update` with --source-root or switch the blackdog handler source_mode"

    if config.source_mode == HANDLER_SOURCE_MODE_LOCAL_OVERRIDE:
        return None, None, (
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="resolve-source",
                target_path=None,
                status=HANDLER_STATUS_BLOCKED,
                message="local-override source mode requires --source-root",
            ),
        ), "rerun with --source-root /path/to/blackdog"

    source_root = _resolve_handler_path(context.project_root, context.project_root, config.managed_source_dir)
    remote = _default_source_remote()
    if (source_root / ".git").is_dir():
        if context.update_managed_source:
            started = time.perf_counter()
            current_branch = _run_git(source_root, "rev-parse", "--abbrev-ref", "HEAD")
            branch = current_branch if current_branch and current_branch != "HEAD" else DEFAULT_SOURCE_BRANCH
            if current_branch == "HEAD":
                _run_command("git", "-C", str(source_root), "checkout", branch)
            _run_command("git", "-C", str(source_root), "pull", "--ff-only", "origin", branch)
            action = HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="update-managed-source",
                target_path=str(source_root),
                status=HANDLER_STATUS_UPDATED,
                message=f"updated managed Blackdog source from {remote}",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
            return source_root, HANDLER_SOURCE_MODE_MANAGED_CHECKOUT, (action,), None
        action = HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="use-managed-source",
            target_path=str(source_root),
            status=HANDLER_STATUS_PRESERVED,
            message="reusing managed Blackdog source checkout",
        )
        return source_root, HANDLER_SOURCE_MODE_MANAGED_CHECKOUT, (action,), None
    if context.operation in {"repo-install", "repo-update"}:
        source_root.parent.mkdir(parents=True, exist_ok=True)
        started = time.perf_counter()
        _run_command("git", "clone", remote, str(source_root))
        action = HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="clone-managed-source",
            target_path=str(source_root),
            status=HANDLER_STATUS_CREATED,
            message=f"cloned managed Blackdog source from {remote}",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
        return source_root, HANDLER_SOURCE_MODE_MANAGED_CHECKOUT, (action,), None
    action = HandlerAction(
        handler_id=config.handler_id,
        kind=config.kind,
        action="require-managed-source",
        target_path=str(source_root),
        status=HANDLER_STATUS_BLOCKED,
        message="managed Blackdog source checkout is missing",
    )
    return source_root, HANDLER_SOURCE_MODE_MANAGED_CHECKOUT, (action,), "run `blackdog repo install` or `blackdog repo update`"


def _build_python_state(
    config: PythonOverlayVenvHandlerConfig,
    context: _HandlerContext,
) -> tuple[_PythonOverlayState, str | None]:
    root_ve_path = _resolve_handler_path(context.project_root, context.project_root, config.root_path)
    root_python_path = root_ve_path / "bin" / "python"
    worktree_ve_path = None if context.worktree_path is None else _resolve_handler_path(
        context.project_root,
        context.worktree_path,
        config.worktree_path,
    )
    worktree_python_path = None if worktree_ve_path is None else worktree_ve_path / "bin" / "python"
    if not root_python_path.is_file():
        remediation = "run `blackdog repo install` or `blackdog repo update`"
        if context.operation in {"repo-install", "repo-update"}:
            remediation = None
        return _PythonOverlayState(
            root_ve_path=root_ve_path,
            root_python_path=root_python_path,
            root_bin_path=root_ve_path / "bin",
            root_site_packages=root_ve_path / "lib",
            worktree_ve_path=worktree_ve_path,
            worktree_python_path=worktree_python_path,
            worktree_bin_path=None if worktree_ve_path is None else worktree_ve_path / "bin",
            worktree_site_packages=None,
            script_policy=config.script_policy,
        ), remediation
    root_site_packages = _site_packages_path(root_ve_path, python_path=root_python_path)
    worktree_site_packages = None
    if worktree_ve_path is not None:
        worktree_site_packages = _site_packages_path(worktree_ve_path, python_path=root_python_path)
    return _PythonOverlayState(
        root_ve_path=root_ve_path,
        root_python_path=root_python_path,
        root_bin_path=root_ve_path / "bin",
        root_site_packages=root_site_packages,
        worktree_ve_path=worktree_ve_path,
        worktree_python_path=worktree_python_path,
        worktree_bin_path=None if worktree_ve_path is None else worktree_ve_path / "bin",
        worktree_site_packages=worktree_site_packages,
        script_policy=config.script_policy,
    ), None


def _preview_python_handler(
    config: PythonOverlayVenvHandlerConfig,
    context: _HandlerContext,
) -> _HandlerStepResult:
    state, remediation = _build_python_state(config, context)
    actions: list[HandlerAction] = []
    if remediation is None:
        actions.append(
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="validate-root-venv",
                target_path=str(state.root_ve_path),
                status=HANDLER_STATUS_PLANNED if context.operation.startswith("worktree-") else HANDLER_STATUS_VALIDATED,
                message="repo-root Python env is available",
            )
        )
    else:
        status = HANDLER_STATUS_PLANNED if context.operation in {"repo-install", "repo-update"} else HANDLER_STATUS_BLOCKED
        actions.append(
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="ensure-root-venv",
                target_path=str(state.root_ve_path),
                status=status,
                message="create or repair the repo-root Python env" if status == HANDLER_STATUS_PLANNED else "repo-root Python env is missing",
            )
        )
    if state.worktree_ve_path is not None:
        if remediation is None:
            actions.extend(
                (
                    HandlerAction(
                        handler_id=config.handler_id,
                        kind=config.kind,
                        action="ensure-worktree-venv",
                        target_path=str(state.worktree_ve_path),
                        status=HANDLER_STATUS_PLANNED,
                        message="create the worktree-local Python env from the repo-root env",
                    ),
                    HandlerAction(
                        handler_id=config.handler_id,
                        kind=config.kind,
                        action="wire-overlay",
                        target_path=str(
                            state.worktree_site_packages / "blackdog-root-overlay.pth"
                        ),
                        status=HANDLER_STATUS_PLANNED,
                        message="overlay repo-root site-packages into the worktree env",
                    ),
                )
            )
            editable_action = _worktree_editable_action(
                config,
                state,
                project_root=context.project_root,
                worktree_path=context.worktree_path,
                execute=False,
            )
            if editable_action is not None:
                actions.append(editable_action)
            actions.append(
                HandlerAction(
                    handler_id=config.handler_id,
                    kind=config.kind,
                    action="root-bin-fallback",
                    target_path=str(state.worktree_bin_path),
                    status=HANDLER_STATUS_PLANNED,
                    message="link missing repo-root tool scripts into the worktree env",
                )
            )
    return _HandlerStepResult(
        ready=remediation is None or context.operation in {"repo-install", "repo-update"},
        actions=tuple(actions),
        state=state,
        remediation=remediation,
    )


def _execute_python_handler(
    config: PythonOverlayVenvHandlerConfig,
    context: _HandlerContext,
) -> _HandlerStepResult:
    state, remediation = _build_python_state(config, context)
    actions: list[HandlerAction] = []
    if remediation is not None and context.operation not in {"repo-install", "repo-update"}:
        return _HandlerStepResult(
            ready=False,
            actions=(
                HandlerAction(
                    handler_id=config.handler_id,
                    kind=config.kind,
                    action="ensure-root-venv",
                    target_path=str(state.root_ve_path),
                    status=HANDLER_STATUS_BLOCKED,
                    message="repo-root Python env is missing",
                ),
            ),
            state=state,
            remediation=remediation,
        )
    if not state.root_python_path.is_file():
        started = time.perf_counter()
        state.root_ve_path.parent.mkdir(parents=True, exist_ok=True)
        _run_command(sys.executable, "-m", "venv", str(state.root_ve_path))
        actions.append(
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="create-root-venv",
                target_path=str(state.root_ve_path),
                status=HANDLER_STATUS_CREATED,
                message="created the repo-root Python env",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        state, _ = _build_python_state(config, context)
    else:
        actions.append(
            _timed_action(
                handler_id=config.handler_id,
                kind=config.kind,
                action="validate-root-venv",
                target_path=state.root_ve_path,
                message="validated the repo-root Python env",
                callback=lambda: _run_command(str(state.root_python_path), "-c", "import sys; print(sys.prefix)"),
            )
        )
    if state.worktree_ve_path is None:
        return _HandlerStepResult(ready=True, actions=tuple(actions), state=state)
    if state.worktree_python_path is None:
        raise HandlerError("missing worktree Python path")
    if not state.worktree_python_path.is_file():
        started = time.perf_counter()
        state.worktree_ve_path.parent.mkdir(parents=True, exist_ok=True)
        _run_command(str(state.root_python_path), "-m", "venv", str(state.worktree_ve_path))
        actions.append(
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="create-worktree-venv",
                target_path=str(state.worktree_ve_path),
                status=HANDLER_STATUS_CREATED,
                message="created the worktree-local Python env from the repo-root env",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        state, _ = _build_python_state(config, context)
    else:
        actions.append(
            _timed_action(
                handler_id=config.handler_id,
                kind=config.kind,
                action="validate-worktree-venv",
                target_path=state.worktree_ve_path,
                message="validated the worktree-local Python env",
                callback=lambda: _run_command(str(state.worktree_python_path), "-c", "import sys; print(sys.prefix)"),
            )
        )
    if state.worktree_site_packages is None or state.worktree_bin_path is None:
        raise HandlerError("missing worktree overlay paths")
    overlay_path = state.worktree_site_packages / "blackdog-root-overlay.pth"
    started = time.perf_counter()
    overlay_status = _write_text_if_changed(overlay_path, str(state.root_site_packages) + "\n")
    actions.append(
        HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="wire-overlay",
            target_path=str(overlay_path),
            status=overlay_status,
            message="wired repo-root site-packages into the worktree env",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    editable_action = _worktree_editable_action(
        config,
        state,
        project_root=context.project_root,
        worktree_path=context.worktree_path,
        execute=True,
    )
    if editable_action is not None:
        actions.append(editable_action)
    started = time.perf_counter()
    created_links, preserved_links = _symlink_root_bin_fallback(state.root_bin_path, state.worktree_bin_path)
    status = HANDLER_STATUS_CREATED if created_links else HANDLER_STATUS_PRESERVED
    message = (
        f"linked {created_links} repo-root tool scripts into the worktree env"
        if created_links
        else f"preserved {preserved_links} repo-root tool script fallbacks"
    )
    actions.append(
        HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="root-bin-fallback",
            target_path=str(state.worktree_bin_path),
            status=status,
            message=message,
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    return _HandlerStepResult(ready=True, actions=tuple(actions), state=state)


def _write_blackdog_launcher(python_path: Path, launcher_path: Path, *, source_root: Path | None) -> str:
    return _write_text_if_changed(
        launcher_path,
        _blackdog_launcher_text(python_path, source_root=source_root),
        executable=True,
    )


def _blackdog_launcher_text(python_path: Path, *, source_root: Path | None) -> str:
    if source_root is None:
        return (
            "#!/bin/sh\n"
            f"exec {shlex.quote(str(python_path))} -m blackdog_cli \"$@\"\n"
        )
    return (
        "#!/bin/sh\n"
        f"PYTHONPATH={shlex.quote(str(source_root / 'src'))}"
        '${PYTHONPATH:+":$PYTHONPATH"} '
        f"exec {shlex.quote(str(python_path))} -m blackdog_cli \"$@\"\n"
    )


def _preview_blackdog_runtime_handler(
    config: BlackdogRuntimeHandlerConfig,
    context: _HandlerContext,
    prior_state: _PythonOverlayState | None,
) -> _HandlerStepResult:
    if prior_state is None:
        raise HandlerError("blackdog-runtime requires the python handler output")
    base_root = context.worktree_path or context.project_root
    launcher_path = _resolve_handler_path(context.project_root, base_root, config.launcher_path)
    source_root, source_mode, source_actions, remediation = _ensure_managed_source_checkout(context, config)
    runtime_mode = None
    if remediation is None:
        runtime_mode = (
            HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE
            if context.worktree_path is not None and source_mode == HANDLER_SOURCE_MODE_TARGET_REPO
            else HANDLER_INSTALL_MODE_LAUNCHER_SHIM
        )
    actions = list(source_actions)
    if remediation is None:
        actions.append(
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="write-blackdog-launcher",
                target_path=str(launcher_path),
                status=HANDLER_STATUS_PLANNED,
                message="write the worktree-local or repo-local Blackdog launcher",
            )
        )
        if runtime_mode == HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE and prior_state.worktree_site_packages is not None:
            actions.append(
                HandlerAction(
                    handler_id=config.handler_id,
                    kind=config.kind,
                    action="write-worktree-source-overlay",
                    target_path=str(prior_state.worktree_site_packages / "blackdog-worktree-source.pth"),
                    status=HANDLER_STATUS_PLANNED,
                    message="overlay the task worktree source into the worktree env",
                )
            )
    return _HandlerStepResult(
        ready=remediation is None,
        actions=tuple(actions),
        state=_BlackdogRuntimeState(
            blackdog_path=launcher_path,
            source_root=source_root,
            source_mode=source_mode,
            runtime_mode=runtime_mode,
        ),
        remediation=remediation,
    )


def _execute_blackdog_runtime_handler(
    config: BlackdogRuntimeHandlerConfig,
    context: _HandlerContext,
    prior_state: _PythonOverlayState | None,
) -> _HandlerStepResult:
    preview = _preview_blackdog_runtime_handler(config, context, prior_state)
    state = preview.state
    if not isinstance(state, _BlackdogRuntimeState):
        raise HandlerError("blackdog-runtime produced an invalid state")
    if not preview.ready:
        return preview
    python_path = (
        prior_state.worktree_python_path
        if context.worktree_path is not None
        else prior_state.root_python_path
    )
    if python_path is None:
        raise HandlerError("blackdog-runtime could not resolve a Python interpreter")
    actions = list(action for action in preview.actions if action.action == "resolve-source" and action.status != HANDLER_STATUS_PLANNED)
    if state.runtime_mode == HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE and prior_state.worktree_site_packages is not None:
        overlay_path = prior_state.worktree_site_packages / "blackdog-worktree-source.pth"
        if context.worktree_path is None:
            raise HandlerError("editable worktree source mode requires a worktree path")
        started = time.perf_counter()
        overlay_status = _write_text_if_changed(overlay_path, str(context.worktree_path / "src") + "\n")
        actions.append(
            HandlerAction(
                handler_id=config.handler_id,
                kind=config.kind,
                action="write-worktree-source-overlay",
                target_path=str(overlay_path),
                status=overlay_status,
                message="overlaid the task worktree Blackdog source into the worktree env",
                elapsed_ms=int((time.perf_counter() - started) * 1000),
            )
        )
        source_root = None
    else:
        source_root = state.source_root
    started = time.perf_counter()
    launcher_status = _write_blackdog_launcher(python_path, state.blackdog_path, source_root=source_root)
    actions.append(
        HandlerAction(
            handler_id=config.handler_id,
            kind=config.kind,
            action="write-blackdog-launcher",
            target_path=str(state.blackdog_path),
            status=launcher_status,
            message="wrote the Blackdog launcher",
            elapsed_ms=int((time.perf_counter() - started) * 1000),
        )
    )
    return _HandlerStepResult(
        ready=True,
        actions=tuple(actions),
        state=state,
    )


def _summarize(
    *,
    ready: bool,
    actions: tuple[HandlerAction, ...],
    remediation: str | None,
    python_state: _PythonOverlayState | None,
    runtime_state: _BlackdogRuntimeState | None,
) -> HandlerPlanSummary:
    return HandlerPlanSummary(
        ready=ready,
        actions=actions,
        remediation=remediation,
        root_ve_path=str(python_state.root_ve_path) if python_state is not None else None,
        root_python_path=str(python_state.root_python_path) if python_state is not None else None,
        worktree_ve_path=str(python_state.worktree_ve_path) if python_state is not None and python_state.worktree_ve_path is not None else None,
        worktree_python_path=str(python_state.worktree_python_path) if python_state is not None and python_state.worktree_python_path is not None else None,
        blackdog_path=str(runtime_state.blackdog_path) if runtime_state is not None else None,
        source_root=str(runtime_state.source_root) if runtime_state is not None and runtime_state.source_root is not None else None,
        source_mode=runtime_state.source_mode if runtime_state is not None else None,
        runtime_mode=runtime_state.runtime_mode if runtime_state is not None else None,
        script_policy=python_state.script_policy if python_state is not None else None,
    )


def _run_handlers(context: _HandlerContext, *, execute: bool) -> HandlerPlanSummary:
    actions: list[HandlerAction] = []
    remediation: str | None = None
    ready = True
    python_state: _PythonOverlayState | None = None
    runtime_state: _BlackdogRuntimeState | None = None
    for handler in _ordered_handlers(context.profile):
        if handler.kind == HANDLER_KIND_PYTHON_OVERLAY_VENV:
            result = (
                _execute_python_handler(handler, context)
                if execute
                else _preview_python_handler(handler, context)
            )
            if isinstance(result.state, _PythonOverlayState):
                python_state = result.state
        elif handler.kind == HANDLER_KIND_BLACKDOG_RUNTIME:
            result = (
                _execute_blackdog_runtime_handler(handler, context, python_state)
                if execute
                else _preview_blackdog_runtime_handler(handler, context, python_state)
            )
            if isinstance(result.state, _BlackdogRuntimeState):
                runtime_state = result.state
        else:
            raise HandlerError(f"unsupported handler kind: {handler.kind}")
        actions.extend(result.actions)
        ready = ready and result.ready
        remediation = remediation or result.remediation
    return _summarize(
        ready=ready,
        actions=tuple(actions),
        remediation=remediation,
        python_state=python_state,
        runtime_state=runtime_state,
    )


def plan_repo_handlers(
    profile: RepoProfile,
    *,
    operation: str,
    source_root: str | None = None,
    update_managed_source: bool = False,
) -> HandlerPlanSummary:
    override = None if source_root is None else Path(source_root)
    context = _HandlerContext(
        profile=profile,
        operation=operation,
        project_root=profile.paths.project_root.resolve(),
        source_root_override=None if override is None else override.resolve(),
        update_managed_source=update_managed_source,
    )
    return _run_handlers(context, execute=False)


def execute_repo_handlers(
    profile: RepoProfile,
    *,
    operation: str,
    source_root: str | None = None,
    update_managed_source: bool = False,
) -> HandlerPlanSummary:
    override = None if source_root is None else Path(source_root)
    context = _HandlerContext(
        profile=profile,
        operation=operation,
        project_root=profile.paths.project_root.resolve(),
        source_root_override=None if override is None else override.resolve(),
        update_managed_source=update_managed_source,
    )
    return _run_handlers(context, execute=True)


def plan_worktree_handlers(profile: RepoProfile, *, worktree_path: Path) -> HandlerPlanSummary:
    context = _HandlerContext(
        profile=profile,
        operation="worktree-preview",
        project_root=profile.paths.project_root.resolve(),
        worktree_path=worktree_path.resolve(),
        source_root_override=_current_blackdog_source_root(),
    )
    return _run_handlers(context, execute=False)


def execute_worktree_handlers(profile: RepoProfile, *, worktree_path: Path) -> HandlerPlanSummary:
    context = _HandlerContext(
        profile=profile,
        operation="worktree-start",
        project_root=profile.paths.project_root.resolve(),
        worktree_path=worktree_path.resolve(),
        source_root_override=_current_blackdog_source_root(),
    )
    return _run_handlers(context, execute=True)


def validate_existing_worktree_handlers(
    profile: RepoProfile,
    *,
    worktree_path: Path,
) -> HandlerPlanSummary:
    """Prove retained worktree handler outputs without creating or rewriting them."""

    context = _HandlerContext(
        profile=profile,
        operation="worktree-adopt",
        project_root=profile.paths.project_root.resolve(),
        worktree_path=worktree_path.resolve(),
        source_root_override=_current_blackdog_source_root(),
    )
    python_state: _PythonOverlayState | None = None
    runtime_state: _BlackdogRuntimeState | None = None
    actions: list[HandlerAction] = []
    blockers: list[str] = []

    for handler in _ordered_handlers(profile):
        if handler.kind == HANDLER_KIND_PYTHON_OVERLAY_VENV:
            if not isinstance(handler, PythonOverlayVenvHandlerConfig):
                raise HandlerError("python handler config has an invalid type")
            state, remediation = _build_python_state(handler, context)
            python_state = state
            checks: list[tuple[str, Path | None, bool]] = [
                ("validate-root-python", state.root_python_path, True),
                ("validate-worktree-python", state.worktree_python_path, True),
                ("validate-worktree-bin", state.worktree_bin_path, False),
                ("validate-worktree-site-packages", state.worktree_site_packages, False),
            ]
            if remediation is not None:
                blockers.append(remediation)
            for action_name, target, require_file in checks:
                exists = target is not None and (target.is_file() if require_file else target.is_dir())
                actions.append(
                    HandlerAction(
                        handler_id=handler.handler_id,
                        kind=handler.kind,
                        action=action_name,
                        target_path=str(target) if target is not None else None,
                        status=HANDLER_STATUS_VALIDATED if exists else HANDLER_STATUS_BLOCKED,
                        message=(
                            f"validated retained handler output {target}"
                            if exists
                            else f"retained handler output is missing: {target}"
                        ),
                    )
                )
                if not exists:
                    blockers.append(action_name)
            python_runtime_valid = False
            if (
                state.worktree_python_path is not None
                and state.worktree_ve_path is not None
                and state.worktree_python_path.is_file()
                and os.access(state.worktree_python_path, os.X_OK)
            ):
                try:
                    observed_prefix = _run_command(
                        str(state.worktree_python_path),
                        "-c",
                        "import pathlib, sys; print(pathlib.Path(sys.prefix).resolve())",
                    )
                    python_runtime_valid = (
                        Path(observed_prefix).resolve() == state.worktree_ve_path.resolve()
                    )
                except HandlerError:
                    python_runtime_valid = False
            actions.append(
                HandlerAction(
                    handler_id=handler.handler_id,
                    kind=handler.kind,
                    action="validate-worktree-python-runtime",
                    target_path=(
                        str(state.worktree_python_path)
                        if state.worktree_python_path is not None
                        else None
                    ),
                    status=(
                        HANDLER_STATUS_VALIDATED
                        if python_runtime_valid
                        else HANDLER_STATUS_BLOCKED
                    ),
                    message=(
                        "validated runnable worktree Python prefix"
                        if python_runtime_valid
                        else "worktree Python is not runnable with the expected environment prefix"
                    ),
                )
            )
            if not python_runtime_valid:
                blockers.append("validate-worktree-python-runtime")
            if state.worktree_site_packages is not None:
                root_overlay = state.worktree_site_packages / "blackdog-root-overlay.pth"
                expected_overlay = str(state.root_site_packages) + "\n"
                overlay_valid = (
                    root_overlay.is_file()
                    and root_overlay.read_text(encoding="utf-8") == expected_overlay
                )
                actions.append(
                    HandlerAction(
                        handler_id=handler.handler_id,
                        kind=handler.kind,
                        action="validate-root-overlay",
                        target_path=str(root_overlay),
                        status=HANDLER_STATUS_VALIDATED if overlay_valid else HANDLER_STATUS_BLOCKED,
                        message=(
                            "validated retained repo-root Python overlay"
                            if overlay_valid
                            else "retained repo-root Python overlay is missing or changed"
                        ),
                    )
                )
                if not overlay_valid:
                    blockers.append("validate-root-overlay")
                editable_paths = _editable_source_paths(
                    state.root_site_packages,
                    project_root=context.project_root,
                    worktree_path=context.worktree_path,
                    require_mapped_exists=True,
                )
                editable_overlay = (
                    state.worktree_site_packages / _WORKTREE_EDITABLE_OVERLAY
                )
                expected_editable = "".join(f"{path}\n" for path in editable_paths)
                editable_valid = (
                    editable_overlay.is_file()
                    and editable_overlay.read_text(encoding="utf-8")
                    == expected_editable
                    if editable_paths
                    else not editable_overlay.exists()
                )
                actions.append(
                    HandlerAction(
                        handler_id=handler.handler_id,
                        kind=handler.kind,
                        action="validate-worktree-editables",
                        target_path=str(editable_overlay),
                        status=(
                            HANDLER_STATUS_VALIDATED
                            if editable_valid
                            else HANDLER_STATUS_BLOCKED
                        ),
                        message=(
                            "validated retained worktree editable overlay"
                            if editable_valid
                            else "retained worktree editable overlay is missing or changed"
                        ),
                    )
                )
                if not editable_valid:
                    blockers.append("validate-worktree-editables")
            if state.worktree_bin_path is not None:
                invalid_fallbacks: list[str] = []
                if not state.root_bin_path.is_dir():
                    invalid_fallbacks.append("root-bin-missing")
                else:
                    for root_item in sorted(state.root_bin_path.iterdir()):
                        if root_item.name in _ROOT_BIN_FALLBACK_EXCLUDES:
                            continue
                        if not root_item.is_file() and not root_item.is_symlink():
                            continue
                        candidate = state.worktree_bin_path / root_item.name
                        if not candidate.exists():
                            invalid_fallbacks.append(root_item.name)
                            continue
                        if candidate.is_symlink() and candidate.resolve() != root_item.resolve():
                            invalid_fallbacks.append(root_item.name)
                fallbacks_valid = not invalid_fallbacks
                actions.append(
                    HandlerAction(
                        handler_id=handler.handler_id,
                        kind=handler.kind,
                        action="validate-root-bin-fallback",
                        target_path=str(state.worktree_bin_path),
                        status=(
                            HANDLER_STATUS_VALIDATED
                            if fallbacks_valid
                            else HANDLER_STATUS_BLOCKED
                        ),
                        message=(
                            "validated retained repo-root tool fallbacks"
                            if fallbacks_valid
                            else "retained repo-root tool fallbacks are missing or changed: "
                            + ", ".join(invalid_fallbacks)
                        ),
                    )
                )
                if not fallbacks_valid:
                    blockers.append("validate-root-bin-fallback")
        elif handler.kind == HANDLER_KIND_BLACKDOG_RUNTIME:
            if not isinstance(handler, BlackdogRuntimeHandlerConfig) or python_state is None:
                raise HandlerError("blackdog-runtime requires validated Python handler state")
            preview = _preview_blackdog_runtime_handler(handler, context, python_state)
            if not isinstance(preview.state, _BlackdogRuntimeState):
                raise HandlerError("blackdog-runtime produced an invalid retained state")
            runtime_state = preview.state
            python_path = python_state.worktree_python_path
            if python_path is None:
                raise HandlerError("retained worktree Python interpreter is missing")
            source_root = (
                None
                if runtime_state.runtime_mode == HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE
                else runtime_state.source_root
            )
            expected_launcher = _blackdog_launcher_text(python_path, source_root=source_root)
            launcher_valid = (
                runtime_state.blackdog_path.is_file()
                and os.access(runtime_state.blackdog_path, os.X_OK)
                and runtime_state.blackdog_path.read_text(encoding="utf-8") == expected_launcher
            )
            actions.append(
                HandlerAction(
                    handler_id=handler.handler_id,
                    kind=handler.kind,
                    action="validate-blackdog-launcher",
                    target_path=str(runtime_state.blackdog_path),
                    status=HANDLER_STATUS_VALIDATED if launcher_valid else HANDLER_STATUS_BLOCKED,
                    message=(
                        "validated retained Blackdog launcher"
                        if launcher_valid
                        else "retained Blackdog launcher is missing or changed"
                    ),
                )
            )
            if not launcher_valid:
                blockers.append("validate-blackdog-launcher")
            launcher_runtime_valid = False
            if launcher_valid:
                try:
                    _run_command(str(runtime_state.blackdog_path), "--help")
                    launcher_runtime_valid = True
                except HandlerError:
                    launcher_runtime_valid = False
            actions.append(
                HandlerAction(
                    handler_id=handler.handler_id,
                    kind=handler.kind,
                    action="validate-blackdog-launcher-runtime",
                    target_path=str(runtime_state.blackdog_path),
                    status=(
                        HANDLER_STATUS_VALIDATED
                        if launcher_runtime_valid
                        else HANDLER_STATUS_BLOCKED
                    ),
                    message=(
                        "validated retained Blackdog launcher execution"
                        if launcher_runtime_valid
                        else "retained Blackdog launcher does not execute successfully"
                    ),
                )
            )
            if not launcher_runtime_valid:
                blockers.append("validate-blackdog-launcher-runtime")
            if (
                runtime_state.runtime_mode == HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE
                and python_state.worktree_site_packages is not None
            ):
                source_overlay = python_state.worktree_site_packages / "blackdog-worktree-source.pth"
                expected_source = str(worktree_path.resolve() / "src") + "\n"
                source_valid = (
                    source_overlay.is_file()
                    and source_overlay.read_text(encoding="utf-8") == expected_source
                )
                actions.append(
                    HandlerAction(
                        handler_id=handler.handler_id,
                        kind=handler.kind,
                        action="validate-worktree-source-overlay",
                        target_path=str(source_overlay),
                        status=HANDLER_STATUS_VALIDATED if source_valid else HANDLER_STATUS_BLOCKED,
                        message=(
                            "validated retained worktree source overlay"
                            if source_valid
                            else "retained worktree source overlay is missing or changed"
                        ),
                    )
                )
                if not source_valid:
                    blockers.append("validate-worktree-source-overlay")
            expected_import_root = (
                worktree_path.resolve() / "src"
                if runtime_state.runtime_mode
                == HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE
                else (
                    runtime_state.source_root.resolve() / "src"
                    if runtime_state.source_root is not None
                    else None
                )
            )
            import_origin_valid = False
            if expected_import_root is not None:
                try:
                    probe_env = None
                    if (
                        runtime_state.runtime_mode
                        != HANDLER_INSTALL_MODE_EDITABLE_WORKTREE_SOURCE
                    ):
                        probe_env = dict(os.environ)
                        source_path = str(expected_import_root)
                        existing_pythonpath = probe_env.get("PYTHONPATH")
                        probe_env["PYTHONPATH"] = (
                            source_path
                            if not existing_pythonpath
                            else source_path + os.pathsep + existing_pythonpath
                        )
                    observed = _run_command(
                        str(python_path),
                        "-c",
                        (
                            "import pathlib, blackdog, blackdog_cli; "
                            "print(pathlib.Path(blackdog.__file__).resolve()); "
                            "print(pathlib.Path(blackdog_cli.__file__).resolve())"
                        ),
                        env=probe_env,
                    ).splitlines()
                    import_origin_valid = len(observed) == 2 and all(
                        Path(item).resolve().is_relative_to(expected_import_root)
                        for item in observed
                    )
                except (HandlerError, OSError):
                    import_origin_valid = False
            actions.append(
                HandlerAction(
                    handler_id=handler.handler_id,
                    kind=handler.kind,
                    action="validate-blackdog-import-origin",
                    target_path=(
                        str(expected_import_root)
                        if expected_import_root is not None
                        else None
                    ),
                    status=(
                        HANDLER_STATUS_VALIDATED
                        if import_origin_valid
                        else HANDLER_STATUS_BLOCKED
                    ),
                    message=(
                        "validated retained Blackdog import origin"
                        if import_origin_valid
                        else "retained Python does not import Blackdog from the expected source"
                    ),
                )
            )
            if not import_origin_valid:
                blockers.append("validate-blackdog-import-origin")
        else:
            raise HandlerError(f"unsupported handler kind: {handler.kind}")

    return _summarize(
        ready=not blockers,
        actions=tuple(actions),
        remediation="; ".join(dict.fromkeys(blockers)) if blockers else None,
        python_state=python_state,
        runtime_state=runtime_state,
    )


__all__ = [
    "DEFAULT_SOURCE_REMOTE",
    "HANDLER_STATUS_BLOCKED",
    "HANDLER_STATUS_CREATED",
    "HANDLER_STATUS_PLANNED",
    "HANDLER_STATUS_PRESERVED",
    "HANDLER_STATUS_UPDATED",
    "HANDLER_STATUS_VALIDATED",
    "HandlerAction",
    "HandlerError",
    "HandlerPlanSummary",
    "execute_repo_handlers",
    "execute_worktree_handlers",
    "validate_existing_worktree_handlers",
    "plan_repo_handlers",
    "plan_worktree_handlers",
]
