# Blackdog

Blackdog is a machine-native task and attempt runtime for AI-driven local
development. It gives agents an isolated task-worktree workflow, durable attempt
history, repo-local setup receipts, and read models for status, recovery, and
Codex-session coverage.

## Packages

- `blackdog_core`: durable task runtime contract, typed
  semantics, and derived read models.
- `blackdog`: task orchestration and repo lifecycle workflows on
  top of the core contract.
- `blackdog_cli`: thin parser/help/dispatch layer for the `blackdog`
  executable.

See [Architecture](docs/ARCHITECTURE.md) for package boundaries and guarantees.

## Repo Use

Blackdog requires Python 3.11 or newer and Git, with no project virtual
environment or third-party Python dependencies. In this source repository use
`./scripts/blackdog`; installed consumers use the standalone `blackdog.pyz`
archive (or install it on `PATH` as `blackdog`).

Install a persistent user command from this checkout:

```sh
./scripts/blackdog self install
blackdog version
```

The default command is `~/.local/bin/blackdog`; installation reports whether
the current `PATH` can find it and prints the needed `PATH` setup when missing.
Use `--bin-dir` for another persistent user-writable directory already on your
terminal and agent `PATH`. No administrator access or project environment is
required. The command retains its own immutable archive, so the source checkout
can be removed. See the [installation details](docs/RUNTIME_DISTRIBUTION.md).

Implementation edits belong in `workspace role: task`. Start with
`blackdog task begin`, use the exact workspace executable returned in
`setup_receipt.workspace_blackdog_path`, validate, then land through
`blackdog task land`. The task's recorded target branch remains authoritative.

The installed repository instructions ask the host to select applicable
[workflow guidance](docs/WORKFLOW_GUIDANCE.md) before admission, including for
short requests. Blackdog snapshots the selected text with the execution prompt;
typed evidence keeps downstream outcomes and workflow compliance distinct.

## Install And Layering Model

- `blackdog.toml` owns control paths, routed docs, validation, and handlers.
- `AGENTS.md` and `.codex/skills/<repo-slug>/SKILL.md` hold the managed contract.
- Generated guidance is maintained with that contract; repository refinements
  remain outside generated files. The host owns semantic selection and execution.
- The default `installed-runtime` handler stores an immutable release beneath
  the private control root, addressed by its SHA-256. Recovery commands name
  this exact archive, so removing a task workspace or updating Blackdog does
  not invalidate previously emitted recovery commands.
- Blackdog self-development deliberately uses `scripts/blackdog` from the
  returned task checkout. Recovery uses the standalone control-root snapshot.
- The Python project handler is optional. Existing explicit Python/legacy
  source handlers remain supported; install and update preserve them and their
  environments. Blackdog never deletes an existing `.VE` during this upgrade.

Run `make release` to create `dist/blackdog.pyz` and its checksum. The archive
contains all three Blackdog packages and a versioned source manifest. It uses
system Python in isolated mode without site packages. It does **not** bundle
Python. See [runtime distribution](docs/RUNTIME_DISTRIBUTION.md) for installation,
upgrade, packaging, and acceptance details.

## Install And Update Runbook

For an existing repo, run `blackdog repo analyze` first. If the repo is not
installed, run `blackdog repo install`; if it is already installed and the
Blackdog runtime should move forward, run `blackdog repo update`. This selects
the invoking user release and refreshes the repository's managed instructions.
`blackdog repo refresh` only regenerates instructions using the selected runtime.
Neither command downloads releases. Install a newer archive with its `self install`
command first, or deliberately pass `--source-root` to `repo update` for a local
Blackdog checkout. Ordinary user commands use the repository's selected runtime;
updating the user command alone does not upgrade repositories. `blackdog version`
shows both identities. Explicit archive paths retain their exact version for recovery.

For a new repo, use `blackdog repo scaffold --target-root ...` when Blackdog
should initialize the project and install the normal repo-local contract in one
operation.

After install, update, scaffold, or refresh, inspect `git status --short` and
commit, land, or explicitly report the managed repo-visible changes. Operator
acceptance is the product surface itself: `repo analyze` should report the repo
as Blackdog-backed, and `worktree preflight` should report the expected
workspace role before kept edits.

## Commands

The [documentation index](docs/INDEX.md#shipped-commands) lists shipped commands;
the [CLI reference](docs/CLI.md) describes their arguments and structured results.

## Validation

```bash
make test
```

`make test` runs `make public-check` first. That gate scans Git-tracked and
unignored candidate files for known private markers, personal home paths,
non-example email addresses, and generated local history exports. Additional
machine-local terms can be added to the gitignored
`.public-denylist.local`, one per line.

The Makefile provides developer checks for this source repository. In target
repos, use the installed `blackdog` or the exact workspace executable returned
by Blackdog and follow the [installation runbook](docs/RUNTIME_DISTRIBUTION.md).

## Docs

- [docs/INDEX.md](docs/INDEX.md)
- [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- [docs/CLI.md](docs/CLI.md)
- [docs/FILE_FORMATS.md](docs/FILE_FORMATS.md)
