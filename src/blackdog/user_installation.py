"""User command installation and read-only repository executable selection.

Only the managed user launcher requests dispatch. Explicit archives and source
entrypoints keep their identity, including machine-emitted recovery commands.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import shlex
import shutil
import sys
import zipfile

from blackdog.errors import BlackdogError
from blackdog.runtime_distribution import (
    _publish_bytes, installed_runtime, release_bytes, runtime_archive,
)
from blackdog_core.profile import ConfigError, load_profile


class UserInstallationError(BlackdogError):
    pass


_LAUNCHER_MARKER = "#!/bin/sh\n# Blackdog managed user launcher v1\n"


def command_visibility(entrypoint: Path | None) -> dict[str, object]:
    found = shutil.which("blackdog")
    matches = bool(found and entrypoint and Path(found).resolve() == entrypoint.resolve())
    return {
        "path_command": found,
        "entrypoint": str(entrypoint) if entrypoint else None,
        "status": "visible" if matches else "shadowed" if found and entrypoint else "available" if found else "missing",
        "scope": "current process PATH; other terminal and agent environments may differ",
        "path_setup": f"export PATH={shlex.quote(str(entrypoint.parent))}:\"$PATH\"" if entrypoint and not matches else None,
    }


def install_user(*, bin_dir: Path | None = None, data_dir: Path | None = None) -> dict[str, object]:
    """Atomically replace our launcher, retaining every immutable user release."""
    bin_dir = (bin_dir or Path.home() / ".local" / "bin").expanduser().absolute()
    data_dir = (data_dir or Path.home() / ".local" / "share" / "blackdog").expanduser().absolute()
    launcher = bin_dir / "blackdog"
    if launcher.is_symlink() or (launcher.exists() and (
        not launcher.is_file() or not launcher.read_bytes().startswith(_LAUNCHER_MARKER.encode())
    )):
        raise UserInstallationError(f"refusing to replace an unmanaged command: {launcher}")
    data = release_bytes()
    digest = hashlib.sha256(data).hexdigest()
    archive = data_dir / "runtime" / "sha256" / f"{digest}.pyz"
    if archive.is_symlink() or (archive.exists() and archive.read_bytes() != data):
        raise UserInstallationError("user runtime conflicts with its immutable digest")
    if not archive.exists():
        _publish_bytes(archive, data)
    if not os.access(archive, os.X_OK):
        raise UserInstallationError("user runtime is not executable")
    # The marker is consumed once, before dispatch. No environment sentinel can
    # leak into child commands or accidentally change an explicit recovery path.
    content = _LAUNCHER_MARKER + f"exec {shlex.quote(str(archive))} --user-entrypoint {shlex.quote(str(launcher))} \"$@\"\n"
    _publish_bytes(launcher, content.encode("utf-8"))
    return {
        "schema_version": 1,
        "entrypoint": str(launcher),
        "runtime": archive_description(archive),
        "visibility": command_visibility(launcher),
        "repository_changes": False,
        "notes": ["No release was downloaded. Existing repository selections and older archives are retained.",
                  "Shell startup files were not modified; PATH setup is reported when needed."],
    }


def repository_profile(start: Path):
    for root in (start.resolve(), *start.resolve().parents):
        if (root / "blackdog.toml").exists():
            return load_profile(root, read_only=True)
    return None


def repository_executable(profile) -> Path:
    # Reuse handler selection, including Blackdog self-development and legacy
    # configured launchers. Do not duplicate source-mode policy in this router.
    from blackdog.handlers import plan_repo_handlers

    summary = plan_repo_handlers(profile, operation="repo-refresh")
    executable = Path(summary.blackdog_path) if summary.blackdog_path else None
    if executable is None or not executable.is_file() or not os.access(executable, os.X_OK):
        raise UserInstallationError("repository Blackdog executable is missing; run blackdog repo install --project-root " + shlex.quote(str(profile.paths.project_root)))
    with executable.open("rb") as stream:
        if stream.read(len(_LAUNCHER_MARKER.encode())) == _LAUNCHER_MARKER.encode():
            raise UserInstallationError("repository executable selects a user dispatcher; install a repository runtime to avoid recursive dispatch")
    return executable.resolve()


def dispatch_target(args) -> Path | None:
    """Return a selected executable for a single-repository ordinary command.

    Admission/update and user diagnostics intentionally run the user release.
    Cross-repository reports have no single repository runtime to dispatch to.
    Parsing stays in the CLI, and the original arguments are forwarded unchanged.
    """
    if args.command in {"self", "version", "init", "local-repo"}:
        return None
    if args.command == "repo" and args.repo_command in {"install", "update", "bind", "scaffold", "migrate"}:
        return None
    root = getattr(args, "project_root", None)
    if isinstance(root, list):
        if len(root) != 1 or getattr(args, "root", None) or getattr(args, "registry", False):
            return None
        root = root[0]
    if root is None:
        return None
    profile = repository_profile(Path(root))
    return repository_executable(profile) if profile is not None else None


def dispatch_user_command(args, argv: list[str]) -> bool:
    target = dispatch_target(args)
    if target is None:
        return False
    os.execv(str(target), [str(target), *argv])
    return True  # os.execv does not return; useful to callers with a test double.


def archive_description(path: Path) -> dict[str, object]:
    data = path.read_bytes()
    with zipfile.ZipFile(path) as archive:
        manifest = json.loads(archive.read("blackdog-release.json"))
    return {"kind": "release-archive", "path": str(path.resolve()),
            "version": manifest["version"], "source_sha256": manifest["source_sha256"],
            "archive_sha256": hashlib.sha256(data).hexdigest()}


def version_report(start: Path, *, entrypoint: Path | None = None) -> dict[str, object]:
    from blackdog import __version__

    archive = runtime_archive()
    invoking = archive_description(archive) if archive else {
        "kind": "source", "version": __version__,
        "path": str(Path(__file__).resolve().parents[2]),
        "archive_sha256": None,
    }
    repository = None
    try:
        profile = repository_profile(start)
    except (ConfigError, OSError, ValueError) as exc:
        profile = None
        repository = {"project_root": None, "requested_root": str(start.resolve()),
                      "executable": None, "runtime": None, "error": str(exc)}
    if profile is not None:
        repository = {"project_root": str(profile.paths.project_root),
                      "control_dir": str(profile.paths.control_dir)}
        try:
            target = repository_executable(profile)
            selected = installed_runtime(profile) if any(
                handler.enabled and handler.kind == "blackdog-runtime" and handler.source_mode == "installed-runtime"
                for handler in profile.handlers
            ) else None
            description = archive_description(selected) if selected else None
            repository.update(executable=str(target),
                              runtime=description if selected == target else None,
                              recovery_runtime=description if selected != target else None,
                              execution_mode="release-archive" if selected and target == selected else "configured-source-or-launcher",
                              error=None)
        except BlackdogError as exc:
            repository.update(executable=None, runtime=None, error=str(exc))
    return {"schema_version": 1, "invoking": invoking, "repository": repository,
            "visibility": command_visibility(entrypoint),
            "python": {"executable": sys.executable, "version": sys.version.split()[0]},
            "selection": "repository for ordinary user commands; invoking release for install/update/migrate and user commands; explicit paths never dispatch",
            "updates_fetch_releases": False}


def render_version(report: dict[str, object]) -> str:
    invoking = report["invoking"]
    lines = [f"Blackdog {invoking['version']} ({invoking['kind']})", f"Invoking: {invoking['path']}"]
    if invoking.get("archive_sha256"):
        lines.append(f"Release SHA-256: {invoking['archive_sha256']}")
    repo = report["repository"]
    if repo:
        lines.extend([f"Repository: {repo['project_root']}", f"Selected executable: {repo.get('executable') or repo.get('error')}"])
        if repo.get("execution_mode"):
            lines.append(f"Execution mode: {repo['execution_mode']}")
        if repo.get("runtime"):
            lines.append(f"Repository release: {repo['runtime']['version']} / {repo['runtime']['archive_sha256']}")
        if repo.get("recovery_runtime"):
            lines.append(f"Retained recovery release: {repo['recovery_runtime']['archive_sha256']}")
    else:
        lines.append("Repository: none")
    visibility = report["visibility"]
    lines.append(f"PATH: {visibility['path_command'] or 'blackdog not found'}")
    lines.append("Upgrade this repository with blackdog repo update; no network release fetching is performed.")
    return "\n".join(lines) + "\n"
