# Cleanup, refactoring and maintenance

Blackdog guidance revision: 1

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
