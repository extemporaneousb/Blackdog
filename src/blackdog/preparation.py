"""Reviewed, worktree-owned preparation; receipts are evidence, never task state.

Recipes are trusted repository code, not a sandbox. This executor restricts its
own filesystem effects and inherited environment; it cannot confine arbitrary
programs. Completed environments are reused only in the same checkout, after
identity verification and fresh readiness checks. Incomplete effects block.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import platform
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from typing import Any

from blackdog.git_worktrees import find_primary_worktree
from blackdog_core.profile import RepoProfile, WorktreePreparationHandlerConfig
from blackdog_core.state import atomic_write_text, exclusive_file_lock, now_iso


MAX_FILE_BYTES = 64 * 1024 * 1024
MAX_TOOL_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_BYTES = 512 * 1024 * 1024
MAX_SNAPSHOT_FILES = 30000


class PreparationError(RuntimeError):
    """A declared preparation requirement could not be verified."""


@dataclass(frozen=True)
class PreparationResult:
    ready: bool
    receipt: dict[str, Any]
    reason: str | None = None


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def _git(root: Path, *args: str) -> bytes:
    completed = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False, timeout=30,
    )
    if completed.returncode:
        raise PreparationError(f"cannot verify preparation Git identity ({args[0]})")
    return completed.stdout


def _contained(root: Path, relative: str) -> Path:
    candidate = root / relative
    if not candidate.resolve().is_relative_to(root.resolve()):
        raise PreparationError(f"declared path escapes its owner: {relative}")
    for parent in (candidate, *candidate.parents):
        if parent == root:
            break
        if parent.is_symlink():
            raise PreparationError(f"declared path has a symlink component: {relative}")
    return candidate


def _file(path: Path, *, max_bytes: int = MAX_FILE_BYTES) -> dict[str, Any]:
    info = path.stat()
    if not stat.S_ISREG(info.st_mode) or info.st_size > max_bytes:
        raise PreparationError("preparation inputs must be bounded regular files")
    digest = hashlib.sha256()
    size = 0
    with path.open("rb") as handle:
        while chunk := handle.read(1024 * 1024):
            size += len(chunk)
            if size > max_bytes:
                raise PreparationError("preparation file exceeds its size bound")
            digest.update(chunk)
    after = path.stat()
    if (info.st_ino, info.st_size, info.st_mtime_ns) != (after.st_ino, after.st_size, after.st_mtime_ns):
        raise PreparationError("preparation file changed while being observed")
    return {"sha256": digest.hexdigest(), "size": size, "mode": stat.S_IMODE(after.st_mode)}


def _bounded_entry(rows: dict[str, dict[str, Any]], relative: str, value: dict[str, Any], total: int) -> int:
    total += int(value.get("size", 0))
    if len(rows) >= MAX_SNAPSHOT_FILES or total > MAX_SNAPSHOT_BYTES:
        raise PreparationError("preparation snapshot exceeds supported file or byte bounds")
    rows[relative] = value
    return total


def _source(root: Path) -> dict[str, Any]:
    paths = _git(root, "ls-files", "-z").decode().split("\0")
    rows = {}
    total = 0
    for relative in filter(None, paths):
        path = _contained(root, relative)
        total = _bounded_entry(rows, relative, _file(path) if path.exists() else {"missing": True}, total)
    return {
        "head": _git(root, "rev-parse", "HEAD").decode().strip(),
        "tree": _git(root, "rev-parse", "HEAD^{tree}").decode().strip(),
        "tracked_snapshot_sha256": _digest(rows),
        "tracked_files": len(rows),
    }


def _run(argv: list[str], *, cwd: Path, env: dict[str, str], deadline: float, capture: bool = False) -> str:
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise PreparationError("preparation command budget expired")
    process = subprocess.Popen(
        argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE if capture else subprocess.DEVNULL,
        stderr=subprocess.STDOUT, start_new_session=True,
    )
    output = bytearray()
    try:
        if capture:
            assert process.stdout is not None
            with selectors.DefaultSelector() as selector:
                selector.register(process.stdout, selectors.EVENT_READ)
                while selector.get_map():
                    if time.monotonic() >= deadline:
                        raise PreparationError("preparation command timed out")
                    for key, _ in selector.select(min(0.1, deadline - time.monotonic())):
                        chunk = os.read(key.fileobj.fileno(), 4096)
                        if not chunk:
                            selector.unregister(key.fileobj)
                        else:
                            output.extend(chunk)
                            if len(output) > 4096:
                                raise PreparationError("tool version output exceeds 4096 bytes")
        try:
            returncode = process.wait(timeout=max(0.001, deadline - time.monotonic()))
        except subprocess.TimeoutExpired as exc:
            raise PreparationError("preparation command timed out") from exc
        if returncode:
            raise PreparationError(f"preparation command exited with status {returncode}; command output is not retained")
        # Background services are outside this recipe contract. A surviving
        # process group would make the output snapshot unsafe to publish.
        try:
            os.killpg(process.pid, 0)
        except ProcessLookupError:
            pass
        else:
            raise PreparationError("preparation command left background processes")
        return output.decode("utf-8").strip()
    finally:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
        process.wait()
        if process.stdout is not None:
            process.stdout.close()


def _environment(root: Path, config: WorktreePreparationHandlerConfig, tool_paths: list[Path]) -> dict[str, str]:
    home = _contained(root, config.outputs[0]) / ".home"
    return {
        "PATH": os.pathsep.join(dict.fromkeys(str(path.parent) for path in tool_paths)),
        "HOME": str(home), "TMPDIR": str(home / "tmp"),
        "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
        "PYTHONNOUSERSITE": "1", "PYTHONDONTWRITEBYTECODE": "1",
        "NODE_DISABLE_COMPILE_CACHE": "1",
        "PIP_CONFIG_FILE": os.devnull, "PIP_NO_INPUT": "1", "PIP_DISABLE_PIP_VERSION_CHECK": "1",
        "PIP_NO_INDEX": "1", "npm_config_offline": "true", "npm_config_audit": "false",
        "npm_config_fund": "false", "npm_config_update_notifier": "false", "npm_config_logs_max": "0",
        "npm_config_userconfig": str(home / "npm-user-config"),
        "npm_config_globalconfig": str(home / "npm-global-config"),
    }


def _toolchain(root: Path, config: WorktreePreparationHandlerConfig, deadline: float) -> tuple[dict[str, Any], dict[str, str]]:
    resolved = {}
    for tool in config.tools:
        executable = shutil.which(tool.executable)
        if executable is None:
            raise PreparationError(f"required tool is unavailable: {tool.name}")
        resolved[tool.name] = Path(executable).absolute()
    env = _environment(root, config, list(resolved.values()))
    rows = {}
    # Even --version can initialize package-manager caches. Keep probes away
    # from both worktree outputs and machine/user configuration.
    with tempfile.TemporaryDirectory(prefix="blackdog-tool-probe-") as temporary:
        probe_root = Path(temporary)
        probe_env = _environment(probe_root, config, list(resolved.values()))
        Path(probe_env["TMPDIR"]).mkdir(parents=True)
        for tool in config.tools:
            path = resolved[tool.name]
            before = _file(path, max_bytes=MAX_TOOL_BYTES)
            version = _run([str(path), *tool.version_args], cwd=probe_root, env=probe_env, deadline=deadline, capture=True)
            if version != tool.version:
                raise PreparationError(f"required tool version does not match recipe: {tool.name}")
            if before != _file(path, max_bytes=MAX_TOOL_BYTES):
                raise PreparationError(f"tool executable changed during its version probe: {tool.name}")
            rows[tool.name] = {"path": str(path), "resolved_path": str(path.resolve()), "version": version, **before}
    return {"platform": platform.system(), "machine": platform.machine(), "tools": rows}, env


def _inputs(root: Path, primary: Path, config: WorktreePreparationHandlerConfig) -> dict[str, Any]:
    tracked = set(filter(None, _git(root, "ls-files", "-z").decode().split("\0")))
    required = dict.fromkeys(("blackdog.toml", *config.tracked_inputs))
    rows = {}
    for relative in required:
        if relative not in tracked:
            raise PreparationError(f"required preparation input is not tracked: {relative}")
        rows[relative] = _file(_contained(root, relative))
    copied = {}
    for item in config.inputs:
        path = _contained(primary, item.source)
        if item.source in set(filter(None, _git(primary, "ls-files", "-z", "--", item.source).decode().split("\0"))):
            raise PreparationError(f"declared local input must be ignored and untracked: {item.source}")
        if subprocess.run(["git", "-C", str(primary), "check-ignore", "-q", "--", item.source], check=False, timeout=30).returncode:
            raise PreparationError(f"declared local input must be ignored: {item.source}")
        observed = _file(path)
        if observed["sha256"] != item.sha256:
            raise PreparationError(f"declared local input digest differs: {item.source}")
        copied[item.destination] = {"source": item.source, "sha256": item.sha256, "mode": item.mode, "size": observed["size"]}
    return {"tracked": rows, "copied": copied}


def _output_paths(root: Path, config: WorktreePreparationHandlerConfig) -> list[Path]:
    paths = []
    for relative in config.outputs:
        path = _contained(root, relative)
        if _git(root, "ls-files", "-z", "--", relative):
            raise PreparationError(f"preparation output overlaps tracked files: {relative}")
        ignored = subprocess.run(["git", "-C", str(root), "check-ignore", "-q", "--", relative + "/"], check=False, timeout=30)
        if ignored.returncode:
            raise PreparationError(f"preparation output must be ignored: {relative}")
        paths.append(path)
    return paths


def _outputs(root: Path, config: WorktreePreparationHandlerConfig, toolchain: dict[str, Any]) -> dict[str, Any]:
    rows = {}
    total = 0
    allowed_tools = {row["resolved_path"] for row in toolchain["tools"].values()}
    output_paths = _output_paths(root, config)
    tracked = set(filter(None, _git(root, "ls-files", "-z").decode().split("\0")))
    for output in output_paths:
        if not output.is_dir():
            raise PreparationError(f"owned preparation output is missing: {output.relative_to(root)}")
        for directory, dirs, files in os.walk(output, followlinks=False):
            for name in sorted((*dirs, *files)):
                path = Path(directory) / name
                relative = str(path.relative_to(root))
                if path.is_symlink():
                    target = path.resolve()
                    owned = any(target == owned_root or target.is_relative_to(owned_root) for owned_root in output_paths)
                    tracked_file = (target.is_relative_to(root) and str(target.relative_to(root)) in tracked and target.is_file())
                    if not target.exists() or not (owned or tracked_file or str(target) in allowed_tools):
                        raise PreparationError(f"output symlink targets unqualified state: {relative}")
                    value = {"symlink": os.readlink(path), "resolved": str(target)}
                elif path.is_dir():
                    value = {"directory": True, "mode": stat.S_IMODE(path.stat().st_mode)}
                else:
                    value = _file(path)
                total = _bounded_entry(rows, relative, value, total)
    return {"sha256": _digest(rows), "files": len(rows)}


def _command_argv(argv: tuple[str, ...], *, root: Path, config: WorktreePreparationHandlerConfig, toolchain: dict[str, Any]) -> list[str]:
    replacements = {"{worktree}": str(root)}
    replacements.update({"{" + name + "}": row["path"] for name, row in toolchain["tools"].items()})
    result = []
    for argument in argv:
        for token, replacement in replacements.items():
            argument = argument.replace(token, replacement)
        result.append(argument)
    if argv[0].startswith("{worktree}/"):
        relative = argv[0][len("{worktree}/"):]
        if not any(Path(output) in Path(relative).parents for output in config.outputs):
            raise PreparationError("worktree command executable must belong to an owned output")
        path = root / relative
        if not path.resolve().is_relative_to(root) and str(path.resolve()) not in {row["resolved_path"] for row in toolchain["tools"].values()}:
            raise PreparationError("worktree command executable escapes qualified outputs and tools")
    return result


def _read_receipt(path: Path) -> dict[str, Any]:
    if path.stat().st_size > 2 * 1024 * 1024:
        raise PreparationError("preparation evidence exceeds its size bound")
    value = json.loads(path.read_text(encoding="utf-8"))
    if (not isinstance(value, dict) or value.get("schema_version") != 1
            or value.get("status") != "ready" or value.get("phase") != "complete"):
        raise PreparationError("unsupported retained preparation evidence")
    return value


def prepare_worktree(
    profile: RepoProfile, config: WorktreePreparationHandlerConfig, *, worktree: Path,
    execute: bool, task_id: str | None = None, attempt_id: str | None = None,
) -> PreparationResult:
    """Prepare once, or verify retained setup without replaying its commands."""
    root = worktree.resolve()
    primary = find_primary_worktree(profile.paths.project_root)
    identity = _digest({"worktree": str(root), "handler": config.handler_id})
    evidence_root = profile.paths.control_dir / "preparation" / identity
    completed_path = evidence_root / "completed.json"
    intent_path = evidence_root / "intent.json"
    receipt: dict[str, Any] = {
        "schema_version": 1, "handler_id": config.handler_id, "revision": config.revision,
        "task_id": task_id, "attempt_id": attempt_id, "worktree": str(root),
        "recipe_sha256": _digest(asdict(config)), "status": "blocked", "phase": "admission",
        "coverage": "declared inputs and checks; undeclared requirements are unknown",
        "reuse": "none", "checked_at": now_iso(), "checks": [],
        "evidence_path": str(completed_path),
    }
    deadline = time.monotonic() + config.timeout_seconds
    try:
        with exclusive_file_lock(completed_path):
            receipt["source"] = _source(root)
            receipt["inputs"] = _inputs(root, primary, config)
            receipt["toolchain"], env = _toolchain(root, config, deadline)
            receipt["repository"] = _digest(str(Path(_git(root, "rev-parse", "--path-format=absolute", "--git-common-dir").decode().strip()).resolve()))
            prior = _read_receipt(completed_path) if completed_path.exists() else None
            if prior is not None:
                for key in ("recipe_sha256", "inputs", "toolchain", "repository", "worktree"):
                    if prior.get(key) != receipt[key]:
                        raise PreparationError(f"retained preparation identity changed: {key}; preserve outputs and review a new attempt")
                receipt["outputs"] = _outputs(root, config, receipt["toolchain"])
                if prior.get("outputs") != receipt["outputs"]:
                    raise PreparationError("retained preparation outputs changed; preserve outputs and review a new attempt")
                receipt["reuse"] = "verified-worktree"
            else:
                if intent_path.exists():
                    raise PreparationError("preparation effects are indeterminate; retained intent has no completed receipt")
                if not execute:
                    raise PreparationError("completed preparation evidence is missing; setup commands are not replayed")
                output_paths = _output_paths(root, config)
                if any(path.exists() for path in output_paths):
                    raise PreparationError("preparation outputs already exist without owned completion evidence")
                receipt["phase"] = "intent"
                atomic_write_text(intent_path, json.dumps(receipt, sort_keys=True) + "\n")
                for output in output_paths:
                    output.mkdir(parents=True)
                Path(env["TMPDIR"]).mkdir(parents=True)
                for item in config.inputs:
                    source = _contained(primary, item.source)
                    destination = _contained(root, item.destination)
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    with source.open("rb") as incoming, destination.open("xb") as outgoing:
                        shutil.copyfileobj(incoming, outgoing, length=1024 * 1024)
                    destination.chmod(item.mode)
                    if _file(destination)["sha256"] != item.sha256:
                        raise PreparationError("declared input changed during materialization")
                receipt["phase"] = "setup"
                for command in config.setup:
                    receipt["step"] = command.name
                    _run(_command_argv(command.argv, root=root, config=config, toolchain=receipt["toolchain"]), cwd=root, env=env, deadline=deadline)
                receipt["outputs"] = _outputs(root, config, receipt["toolchain"])
            receipt["phase"] = "readiness"
            for command in config.checks:
                receipt["step"] = command.name
                started = time.monotonic_ns()
                _run(_command_argv(command.argv, root=root, config=config, toolchain=receipt["toolchain"]), cwd=root, env=env, deadline=deadline)
                receipt["checks"].append({"name": command.name, "status": "passed", "elapsed_ms": (time.monotonic_ns() - started) // 1_000_000})
            if receipt["source"] != _source(root) or receipt["inputs"] != _inputs(root, primary, config):
                raise PreparationError("source or declared inputs changed during preparation; readiness was discarded")
            if receipt["toolchain"] != _toolchain(root, config, deadline)[0]:
                raise PreparationError("toolchain changed during preparation; readiness was discarded")
            if receipt["outputs"] != _outputs(root, config, receipt["toolchain"]):
                raise PreparationError("readiness commands changed owned outputs; readiness was discarded")
            receipt.update(status="ready", phase="complete")
            if prior is None:
                atomic_write_text(completed_path, json.dumps(receipt, sort_keys=True) + "\n")
            return PreparationResult(True, receipt)
    except (PreparationError, OSError, ValueError, subprocess.SubprocessError, KeyboardInterrupt) as exc:
        reason = str(exc) if not isinstance(exc, KeyboardInterrupt) else "preparation interrupted; effects require inspection"
        if receipt.get("step"):
            reason = f"{receipt['phase']}/{receipt['step']}: {reason}"
        receipt.update(status="blocked", reason=reason)
        return PreparationResult(False, receipt, reason)
