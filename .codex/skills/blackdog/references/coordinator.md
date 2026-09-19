# Coordinated delivery

Blackdog guidance revision: 1

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
