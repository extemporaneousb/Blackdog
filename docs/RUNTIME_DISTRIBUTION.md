# Runtime distribution

Blackdog requires Git and Python 3.11 or newer on Linux or macOS. Its executable
archive includes the three Blackdog packages and no third-party dependencies.
Direct execution uses `python3 -I -S`, so `PYTHONPATH`, current-directory
modules and automatic site-package initialization do not select its imports.
The interpreter is selected through `PATH`; activating an environment can
change that selection. The interpreter, standard library and native libraries
remain trusted system dependencies, not bundled or fully pinned components.
An explicit Python invocation must include `-I -S` to retain isolation;
`python3 blackdog.pyz` bypasses the shebang flags. The accepted
[runtime and worktree preparation contract](WORKTREE_PREPARATION.md) defines
this assurance and the separate repository preparation requirements.
An optional reviewed `worktree-preparation` recipe can create the repository's
task-local environments and verify their inputs, tools, outputs and readiness.
It does not change Blackdog's runtime selection or copy a primary virtual
environment. Its current reuse boundary is one verified task checkout;
cross-task dependency caching remains separate work.

## Build and install

From a source checkout run `make release`. This runs the public-file guard and
builds `dist/blackdog.pyz` plus `dist/blackdog.pyz.sha256`. Source inclusion is
limited to Git-indexed `.py` files in the three packages and the MIT license.
Stage new package files
before building; untracked helper files, caches, tests, private control data,
and prompts are excluded. Package-file and source-root symlinks are rejected.
Identical module bytes produce identical archives, independent of filesystem
timestamps. `blackdog-release.json` inside the archive records schema version,
package version, minimum Python version, and source-content SHA-256.

From a checkout, `./scripts/blackdog self install` builds and installs the current
Git-indexed source. From a downloaded archive, verify the supplied checksum and
preserve executable permission (`chmod +x blackdog.pyz` if needed), then run:

```sh
"/path/to/blackdog.pyz" self install
```

The default user command is `~/.local/bin/blackdog`, backed by a digest-addressed
archive under `~/.local/share/blackdog/runtime/sha256/`. `--bin-dir` and `--data-dir`
select other persistent locations. The installer atomically publishes its owned
launcher, retains older archives, and refuses to overwrite an unrelated command
or symlink. It needs no source checkout after installation. It does not change
shell startup files or repository selections. Rerun `self install` from the new
archive to upgrade the user command; there is no network `self update` command.

Installation reports `visible`, `missing`, or `shadowed` against the current
process `PATH`, plus a shell setup line when needed. Put the chosen bin directory
on both terminal and agent `PATH`, or choose an existing persistent user-writable
bin directory. An already-running application's environment is not changed by
editing shell startup files. Verify `command -v blackdog` and `blackdog version`
in each relevant environment. Administrator access is not required.

An explicit archive path also works, including paths containing spaces:

```sh
"/path/to/blackdog.pyz" repo install --project-root /path/to/repo --json
```

Installation stores the exact archive under
`<control_dir>/runtime/sha256/<archive_sha256>.pyz` and atomically selects it with
`<control_dir>/bin/blackdog`. No source checkout, package download, `.VE`, or pip
installation is needed. Recovery commands contain the absolute digest-addressed
archive path. Updating the selected version preserves older archives and their
executable recovery commands. Every resolver validates ownership and digest,
including migration before normal task loading.

Run `repo update` using the **new** user installation or archive to select its
version and refresh managed instructions in one operation. The refresh executes
the selected runtime, including when `--source-root` supplies a version different
from the caller. An old archive invoking update selects that old version; update
does not fetch remote source. It preserves repository policy and task evidence.
Compatibility is checked before runtime replacement. If instruction refresh fails
after selection, the command reports failure with that partial state; rerunning
the same update repairs it. This is recoverable sequencing, not an atomic
transaction over every repository file or arbitrary environment handler.
`--source-root` deliberately snapshots Git-indexed files from an explicit local
Blackdog checkout. This option is useful for development and carries a distinct
content digest, including any modifications to tracked files.

## Command selection and diagnostics

Only the managed user launcher dispatches commands. For an ordinary command with
one selected repository, it uses existing handler resolution and that repository's
configured executable. This supports ancestor discovery, `--project-root`, custom
control roots and linked worktrees. Missing or corrupt selected runtimes fail
closed rather than falling back to the user version. Blackdog development retains
its checkout-local source entrypoint; legacy configured source handlers remain
authoritative.

User `self` and `version` commands, initialization, repository install/update/bind/
scaffold/migration, and cross-repository reports execute the user release. Help
and argument parsing use the user release's command schema. Keep the user command
at least as recent as the repository versions it manages. Explicit archive and
source paths do not dispatch, so emitted recovery commands keep their exact
version. Migration preview/apply actions emitted by an archive retain the invoking
archive, rather than selecting the repository version being upgraded.

`blackdog version --json` reports the invoking version/content digest, selected
repository archive and executable, source-development mode and its distinct
retained recovery archive, interpreter, and
current command visibility. This read-only diagnostic does not load or migrate
task state. A selected runtime error is reported alongside the user identity.
`blackdog --version` is the concise package-version display; use `version` to
distinguish archives with the same package version.

An update affects managed instructions in the selected checkout. Linked worktrees
sharing a control root also share its runtime selection; their tracked instruction
files remain independent. Use `repo refresh` in another checkout when its managed
files need regeneration, then review and land those changes through its workflow.

## Existing repositories

Existing explicit handlers remain unchanged by install or update. To adopt the
standalone default, change the Blackdog runtime handler to this configuration:

```toml
[[handlers]]
id = "blackdog"
kind = "blackdog-runtime"
enabled = true
source_mode = "installed-runtime"
```

Installed-runtime uses immutable archives for consumers and explicit checkout
source for Blackdog development. The configured `paths.control_dir` determines
the runtime location, including custom control roots. Remove the old
`depends_on = ["python"]`, `launcher_path`, and source/install fields from this
handler. Legacy source modes still accept their existing configuration fields.

Retain the separate `python-overlay-venv` handler when project tools need it.
Disable or remove it only when the repository no longer needs that environment.
Run the new archive's `repo install` or `repo update`, then review/land changed
profile and managed instructions. Environments are not
deleted. `runtime.json`, `events.jsonl`, prompt artifacts, task IDs, and attempt
history are unaffected. Older durable-store upgrades still use the explicit,
digest-guarded `repo migrate` protocol.

## Development and acceptance

In Blackdog itself `scripts/blackdog` selects source relative to that tracked
script, with isolated system Python. A task receipt returns the task checkout's
script, so edits execute before landing. Recovery uses the preserved release
snapshot outside the disposable task checkout.

The release tests exercise reproducibility and version identity, unsupported
Python rejection, source inclusion, symlink escapes, digest corruption,
installation and cleanup without `.VE`, explicit project Python handlers,
source selection, and retained older releases across updates. The release
workflow runs repository checks and external artifact acceptance on Linux/macOS
with Python 3.11 and 3.14. A workflow definition is not evidence that a particular
revision passed CI; completion reports must identify the actual run and SHA.
