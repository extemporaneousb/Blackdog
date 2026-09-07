"""Reproducible Python releases and immutable, environment-independent launchers.

The runtime snapshot belongs to the private control root. Project environments
and disposable worktrees never own the executable used by recovery commands.
"""

from __future__ import annotations

import ast
import hashlib
from importlib.metadata import distribution, PackageNotFoundError
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import zipfile

from blackdog.errors import BlackdogError
from blackdog_core.profile import RepoProfile, load_profile, resolve_config_path


class RuntimeDistributionError(BlackdogError):
    pass


_PACKAGES = ("blackdog", "blackdog_cli", "blackdog_core")
_SHEBANG = b"#!/usr/bin/env -S python3 -I -S\n"
_MAIN = b'''import sys
if sys.version_info < (3, 11):
    raise SystemExit("Blackdog requires Python 3.11 or newer.")
from blackdog_cli.main import main
raise SystemExit(main())
'''


def _read_package_file(path: Path, root: Path) -> bytes:
    path, root = path.absolute(), root.absolute()
    if ".." in path.parts or ".." in root.parts or not path.is_relative_to(root) or path == root:
        raise RuntimeDistributionError("release source file escapes the package root")
    relative = path.relative_to(root)
    for length in range(len(relative.parts) + 1):
        candidate = root.joinpath(*relative.parts[:length])
        if candidate.is_symlink():
            raise RuntimeDistributionError("release source must not contain symlinks")
    if not path.is_file():
        raise RuntimeDistributionError("release source must contain only regular files")
    return path.read_bytes()


def _package_entries(source_root: Path | None) -> dict[str, bytes]:
    current = Path(__file__).resolve().parents[2]
    if source_root is None and (current / "pyproject.toml").is_file():
        source_root = current
    entries: dict[str, bytes] = {}
    if source_root is not None:
        source_root = source_root.resolve()
        result = subprocess.run(
            ["git", "-C", str(source_root), "ls-files", "-z", "--cached", "--",
             *(f"src/{package}" for package in _PACKAGES), "LICENSE"],
            capture_output=True, check=False,
        )
        if result.returncode:
            raise RuntimeDistributionError("source releases require a Git-indexed Blackdog checkout")
        for raw in result.stdout.split(b"\0"):
            if not raw:
                continue
            relative = Path(os.fsdecode(raw))
            if relative.as_posix() == "LICENSE":
                entries["LICENSE"] = _read_package_file(source_root / relative, source_root)
                continue
            if relative.suffix != ".py":
                continue
            name = relative.relative_to("src").as_posix()
            entries[name] = _read_package_file(source_root / relative, source_root / "src")
    else:
        try:
            installed = distribution("blackdog")
        except PackageNotFoundError as exc:
            raise RuntimeDistributionError("Blackdog package has no distribution manifest") from exc
        for relative in installed.files or ():
            if relative.parts[0] in _PACKAGES and relative.suffix == ".py":
                path = Path(installed.locate_file(relative))
                entries[relative.as_posix()] = _read_package_file(path, Path(installed.locate_file("")))
            elif relative.name == "LICENSE" and any(part.endswith(".dist-info") for part in relative.parts):
                entries["LICENSE"] = _read_package_file(Path(installed.locate_file(relative)), Path(installed.locate_file("")))
    required = {f"{package}/__init__.py" for package in _PACKAGES} | {"blackdog_cli/main.py", "blackdog/runtime_distribution.py", "LICENSE"}
    if not required.issubset(entries):
        raise RuntimeDistributionError("release source is incomplete; stage new package files before building")
    return entries


def _release_version(entries: dict[str, bytes]) -> str:
    tree = ast.parse(entries["blackdog/__init__.py"])
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(target, ast.Name) and target.id == "__version__" for target in node.targets):
            value = ast.literal_eval(node.value)
            if isinstance(value, str) and value:
                return value
    raise RuntimeDistributionError("release source has no literal version")


def release_bytes(*, source_root: Path | None = None) -> bytes:
    """Build the same archive from identical module bytes, without timestamps."""
    archive = runtime_archive() if source_root is None else None
    if archive is not None:
        return archive.read_bytes()
    entries = _package_entries(source_root)
    version = _release_version(entries)
    entries["__main__.py"] = _MAIN
    source_hash = hashlib.sha256()
    for name, data in sorted(entries.items()):
        source_hash.update(name.encode("utf-8") + b"\0")
        source_hash.update(hashlib.sha256(data).digest())
    entries["blackdog-release.json"] = (json.dumps({
        "schema_version": 1,
        "version": version,
        "requires_python": ">=3.11",
        "source_sha256": source_hash.hexdigest(),
    }, sort_keys=True, separators=(",", ":")) + "\n").encode("utf-8")
    output = io.BytesIO(_SHEBANG)
    output.seek(0, io.SEEK_END)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        for name, data in sorted(entries.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            archive.writestr(info, data)
    return output.getvalue()


def write_release(target: Path, *, source_root: Path | None = None) -> str:
    data = release_bytes(source_root=source_root)
    _publish_bytes(target, data)
    return hashlib.sha256(data).hexdigest()


def _sync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _publish_bytes(target: Path, data: bytes) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}.", dir=target.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fchmod(stream.fileno(), 0o755)
            os.fsync(stream.fileno())
        os.replace(temporary, target)
        _sync_directory(target.parent)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def installed_runtime(profile: RepoProfile) -> Path | None:
    return _installed_runtime(profile.paths.control_dir)


def _validate_runtime_layout(control_dir: Path) -> None:
    """Check owned publication parents before either inspection or mutation."""
    for relative in ("runtime", "runtime/sha256", "bin"):
        parent = control_dir / relative
        if parent.is_symlink():
            raise RuntimeDistributionError("installed Blackdog runtime directories must not be symlinks")
        if parent.exists() and not parent.is_dir():
            raise RuntimeDistributionError("installed Blackdog runtime parent is not a directory")
        if not parent.resolve().is_relative_to(control_dir.resolve()):
            raise RuntimeDistributionError("installed Blackdog runtime path escapes its control root")


def _installed_runtime(control_dir: Path) -> Path | None:
    _validate_runtime_layout(control_dir)
    launcher = control_dir / "bin" / "blackdog"
    if not launcher.exists() and not launcher.is_symlink():
        return None
    target = launcher.resolve()
    archive_root = (control_dir / "runtime" / "sha256").resolve()
    if not archive_root.is_relative_to(control_dir.resolve()):
        raise RuntimeDistributionError("installed Blackdog archive directory escapes its control root")
    if target.parent != archive_root or target.suffix != ".pyz" or not target.is_file():
        raise RuntimeDistributionError("installed Blackdog runtime reference is invalid")
    if hashlib.sha256(target.read_bytes()).hexdigest() != target.stem:
        raise RuntimeDistributionError("installed Blackdog runtime digest does not match")
    if not os.access(target, os.X_OK):
        raise RuntimeDistributionError("installed Blackdog runtime is not executable")
    return target


def install_runtime(
    profile: RepoProfile, *, source_root: Path | None = None, update: bool = False,
) -> Path:
    existing = installed_runtime(profile)
    if existing is not None and not update:
        return existing
    data = release_bytes(source_root=source_root)
    digest = hashlib.sha256(data).hexdigest()
    target = profile.paths.control_dir / "runtime" / "sha256" / f"{digest}.pyz"
    if target.is_symlink():
        raise RuntimeDistributionError("immutable Blackdog release must not be a symlink")
    if target.exists():
        if target.read_bytes() != data:
            raise RuntimeDistributionError("immutable Blackdog release conflicts with its digest")
    else:
        _publish_bytes(target, data)
    launcher = profile.paths.control_dir / "bin" / "blackdog"
    launcher.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".blackdog-", dir=launcher.parent)
    os.close(descriptor)
    os.unlink(temporary)
    try:
        os.symlink(os.path.relpath(target, launcher.parent), temporary)
        os.replace(temporary, launcher)
        _sync_directory(launcher.parent)
    finally:
        if os.path.lexists(temporary):
            os.unlink(temporary)
    return target.resolve()


def runtime_archive() -> Path | None:
    """Return the running release, independent of argv supplied by the caller."""
    loader = globals().get("__loader__")
    archive = getattr(loader, "archive", None)
    return Path(archive).resolve() if isinstance(archive, str) else None


def runtime_identity() -> dict[str, str] | None:
    archive = runtime_archive()
    if archive is None:
        return None
    return {"kind": "release_sha256", "value": hashlib.sha256(archive.read_bytes()).hexdigest()}


def runtime_executable(project_root: Path, *, profile: RepoProfile | None = None) -> str:
    """Resolve executable authority without PATH guesses or disposable launchers.

    Installed profiles pin the exact archive. Before installation (including
    migration), use the currently executing archive or the primary source
    checkout's tracked bootstrap. Neither resolution mutates repository state.
    """
    if profile is not None:
        installed = installed_runtime(profile)
        if installed is not None:
            return str(installed)
    else:
        control_dir = (
            load_profile(project_root, read_only=True).paths.control_dir
            if (project_root / "blackdog.toml").is_file()
            else resolve_config_path(project_root, "@git-common/blackdog")
        )
        installed = _installed_runtime(control_dir)
        if installed is not None:
            return str(installed)
    archive = runtime_archive()
    if archive is not None:
        return str(archive)
    source_root = Path(__file__).resolve().parents[2]
    primary = subprocess.run(
        ["git", "-C", str(source_root), "worktree", "list", "--porcelain"],
        capture_output=True, text=True, check=False,
    )
    if primary.returncode == 0:
        for line in primary.stdout.splitlines():
            if line.startswith("worktree "):
                candidate = Path(line.removeprefix("worktree "))
                if (candidate / ".git").is_dir() and (candidate / "scripts" / "blackdog").is_file():
                    return str(candidate / "scripts" / "blackdog")
    bootstrap = source_root / "scripts" / "blackdog"
    if bootstrap.is_file():
        return str(bootstrap)
    raise RuntimeDistributionError("Blackdog has no durable executable; install a release archive")
