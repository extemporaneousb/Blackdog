# Runtime distribution

Blackdog requires Git and Python 3.11 or newer on Linux or macOS. Its executable
archive includes the three Blackdog packages and no third-party dependencies.
It runs Python with `-I -S`, so `PYTHONPATH`, user packages, and project virtual
environments do not select its imports. The interpreter remains a system
requirement; this is not a bundled-interpreter binary.

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

Copy the archive to a persistent executable location on `PATH`, naming it
`blackdog`, and preserve executable permission (`chmod +x blackdog` if a download
removed it). Verify the supplied checksum before installation. An explicit
archive path also works, including paths containing spaces:

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

Run `repo update` using the **new** archive to select its version, then run
`repo refresh` to refresh managed instructions. An old archive invoking update
selects that old version; update does not fetch unreviewed remote source.
`--source-root` deliberately snapshots Git-indexed files from an explicit local
Blackdog checkout. This option is useful for development and carries a distinct
content digest, including any modifications to tracked files.

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
Run the new archive's `repo install` or `repo update`, then `repo refresh` and
review/land changed profile and managed instructions. Environments are not
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
