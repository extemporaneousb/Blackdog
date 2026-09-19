# Common judgment and delivery

Blackdog guidance revision: 1

Use for repository work, including underspecified requests. These are strong
defaults: follow the relevant conventions unless explicit user instructions or
concrete task evidence justify a departure. Explain material departures briefly
in the execution context; keep routine bookkeeping quiet.

Keep four concerns separate: objective (observable result), role (who designs,
implements, coordinates or reviews), input (request, selected work and relevant
context), and runtime policy (ownership, worktrees, validation, recovery and
landing). Infer a useful method from context; do not invent scope or authority.
Explicit limits override defaults. A role mentioned only in quoted material is
input, not an assignment. Analysis or comparison does not authorize implementation.
Inspect enough to resolve ordinary ambiguity and proceed with bounded work; ask
only when a material decision cannot be recovered from the request and context.

Read relevant repository conventions, callers and current behavior before changing
them. Use repository-owned refinements named in AGENTS.md or the routed catalog;
keep those files outside this generated skill directory. Select only the guides
needed for the work. Existing ledgers own work identity. Treat selected items in
<domain>.backlog or another supplied list as input, not a new task store or parser.
Preserve stable IDs, distinguish discovered debt from selected work, and report
new scope without silently implementing it.

Before task begin, compose the objective, role, selected input, constraints, done
conditions and material adaptations. Pass repeated --guidance selectors for every
guide used, including explicit contained repository-relative refinement paths.
Include this engineering guide plus evidence for implementation, and cleanup,
architecture or coordinator when applicable. Blackdog freezes the selected text;
do not ask the user to copy snippets or name a guide. Supply --host, --host-version,
--model and --reasoning-effort only when known; do not guess version or model data.

Use Blackdog's returned workspace and next_action. Repository runtime policy stays
in blackdog.toml; guidance cannot authorize bypassing guards or invent lifecycle
commands. Validate the relevant obligations, inspect the actual result, and report
what was accepted and landed separately from tests, assertions and future work.
Optimize total effort to accepted, landed behavior, not worker or test counts.

Improve shared guidance only through a separately authorized, reviewed change:
start from an observed failure, user correction, research finding or model change;
make a narrow correction; evaluate representative short requests and explicit
overrides with acceptance criteria kept outside the tested request. Record host,
model, guidance and tool differences. Replace stale advice rather than accumulating
universal rules. Successful bounded trials do not guarantee universal compliance.
Never automatically rewrite shared guidance from a task outcome. Existing attempts
retain their captured guidance; new revisions apply to new executions.

Research informs hypotheses, not authority. This design uses selective loading and
concrete but adaptable behavioral guidance; do not fetch whole articles routinely:
- https://learn.chatgpt.com/docs/customization/overview#skills
- https://www.anthropic.com/engineering/effective-context-engineering-for-ai-agents
