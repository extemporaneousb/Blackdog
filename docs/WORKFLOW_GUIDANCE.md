# Workflow Guidance

Blackdog distributes working guidance through the managed repository contract
and skill. The host selects relevant guidance before planning or task admission,
including for short requests that do not name a skill or spell out a process.
Selection uses the request, conversation and repository context. Blackdog does
not classify intent, execute a model or assign workers.

## Selection and authority

The compact catalog contains `engineering`, `cleanup`, `architecture`,
`coordinator` and `evidence`. The evidence guide applies to implementation;
load other guides according to the work. Keep the requested outcome,
assigned responsibilities, selected work inputs and runtime policy distinct.
A quoted role name or matching code symbol does not assign a role. Established
defaults may fill in a method; they cannot enlarge the user's scope or authority.

Use `blackdog guidance list` for short names, summaries and example request cues,
and `blackdog guidance show NAME` (or `show --all`) for the exact installed text.
The names index methods, not personas. Example cues help humans express intent;
there are no keyword triggers or measured odds of selection. Explicit constraints
and the whole request still govern interpretation.

Guidance supplies adaptable defaults, not a mandatory worker count or repeated
review sequence. Record material adaptations in the execution prompt with their
task-specific reasons. Inspect bounded uncertainty before asking; ask when an
unresolved ambiguity changes the objective or authorized work. If delegation is
required but unavailable, report that constraint rather than silently taking
over the assigned work. Optional helpers need not block an ordinary task.

Repository policy remains in `blackdog.toml`; exact structured `next_action`
values remain lifecycle authority. Analysis-only work does not acquire an
implementation task merely because a guide applies. Repository-authored guide
refinements belong outside generated files, which install/update/refresh owns.
Shared defaults must not copy private user preferences into public content.

Generated documents live under `.codex/skills/<repo-slug>/references/`, alongside
`catalog.md`. They carry a guidance revision; their exact SHA-256 identifies the
selected content. Built-in metadata and wording have one maintained definition;
the CLI overview and generated agent catalog derive from it. Generated files are
installation artifacts, not independently edited copies. Inspection reads the
installed guide body through the same resolver as admission, and reports missing
files, wording drift and index drift instead of substituting another version.

Keep refinements in a separate repository-owned path. `blackdog guidance add`
registers an existing file under a short name, summary and a few example cues;
the body stays in that file. Its `[[guidance]]` metadata in `blackdog.toml` drives
both CLI discovery and the generated catalog. Adding a name refuses collisions;
replacing an entry or revising wording requires an explicit reviewed edit and
catalog refresh. Registration refuses directories that Blackdog refresh owns,
including obsolete managed skill directories. Direct repository-relative
selectors remain supported without registration. Keep the catalog small and
selective; a new guide should add a distinct useful convention, not another alias
or a generic rule repeated across every task.

## Freeze the selected inputs

Both `task begin` and `prompt preview` accept repeated `--guidance` arguments.
Each value identifies a built-in or registered name, or a repository-relative guide file:

```bash
blackdog prompt preview --project-root . --request "Tidy this module" \
  --guidance engineering --guidance cleanup --show-prompt --json
```

Selections are ordered and bounded to 16 documents, 32 KiB per UTF-8 file and
128 KiB total. Empty, duplicate, missing, absolute, escaping or invalid files
are refused before admission. A built-in ID resolves to its installed guide;
it does not bypass the installed repository contract.

The host composes the operative goal, context, constraints, adaptations and done
condition, preserves the original request separately, and passes its selected
guides at normal admission. Blackdog includes the exact selected text, JSON
encoded to preserve line endings, in the private execution-prompt snapshot and
records its path and SHA-256 in setup
metadata. A mutable path or hash alone is not replayable guidance. An existing
attempt replays its admitted snapshot even after a guide changes or disappears;
new work can deliberately use the revised text.

On `task begin`, supply `--host` and `--host-version` when known, alongside
existing `--model` and `--reasoning-effort`. These are caller declarations, not
runtime discovery or attestation. Leave unavailable values unknown. Full
conversation, child-agent tool history and model/tool configuration remain
host-owned. Preview composes
inputs without starting a task; it cannot establish that an agent obeyed them.

A retry of the same active attempt retains its recorded host context. A new
successor attempt may reuse frozen guidance, but it does not inherit the
predecessor's execution host. Supply freshly known host/version values for the
new attempt; otherwise its host context remains unknown in reports. Guidance
replay preserves execution input, not evidence of where a later attempt ran.

## Observe outcomes and compliance separately

Use schema-2 [typed definitions](OUTCOME_EVIDENCE.md) to distinguish product
`outcome` criteria from workflow `compliance` criteria. State what would count
as success and what evidence can assess each criterion. A landed commit, a
validation pass, a guidance snapshot and a declared compliance assessment are
different facts. Missing definitions, measurements or assessments remain missing.

Record command measurements through `task validate`; record assessments and
explicit intervention observations through `task outcome`. Preserve provenance,
source identity, applicability and denominator coverage. An agent saying that a
step happened is a declaration; a receipt only measures the bounded operation it
actually observes. Do not infer review independence, complete human-intervention
coverage, cost or elapsed work from absent records or overlapping phase clocks.
Schema-2 assessments can preserve a rationale and host-owned evidence locators;
those locators are not fetched or authenticated by Blackdog.

Keep task definitions, validation inputs, model/effort, declared host/version
and selected guidance identities in comparison cohorts. Unknown or changed
context limits comparability. These records support investigation; they do not
make observational cohorts randomized or prove that guidance caused an outcome.

## Improve guidance through review

Candidate changes may come from repeated failures, user corrections, research,
or model/host changes. Record the observed failure or hypothesis, modify the
narrowest relevant guide, then evaluate representative short requests and
held-out paraphrases before accepting the revision. Replace stale advice rather
than accumulating instructions. Outcomes do not automatically rewrite guides.

Keep detailed acceptance criteria with the evaluator, outside the short request
shown to the worker. Include implicit intent, quoted role names, explicit
overrides, bounded ambiguity, unavailable tools, dependent edits, stale
references, target movement and interrupted attempts. Assess guide discovery and
selection, scope preservation, justified adaptation, accepted behavior, user
correction and measured total effort. Correct labels or polished plans alone
are insufficient. Run narrow regressions for the changed guide and nearby
overrides; retain held-out cases to detect overfitting.

Installation/replay checks establish distribution and provenance. Behavioral
trials are still needed for claims about sparse-request selection or better
engineering outcomes, with model/host/context and sample limits stated.

The design draws on [OpenAI customization and skills](https://learn.chatgpt.com/docs/customization/overview#skills),
[Anthropic context engineering](https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents)
and [Anthropic agent patterns](https://www.anthropic.com/engineering/building-effective-agents).
These are research inputs for maintained guidance, not evidence of Blackdog's
effectiveness. Routine tasks need not repeat the research; a reviewed change
should record the relevant sources and model/tool applicability.

## Current work and historical obligations

Use [the index](INDEX.md) for current contracts. Historical milestone and
acceptance documents retain their original evidence limits. The live
[residual ledger](RESIDUAL_WORK.md) retains accepted but undelivered preparation
requirements and stable `PREP-*` identities. Guidance delivery or documentation
cleanup does not close those obligations or authorize unrelated work.
