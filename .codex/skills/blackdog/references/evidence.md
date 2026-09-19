# Outcome and compliance evidence

Blackdog guidance revision: 1

Use for implementation alongside engineering and applicable workflow guidance.
The attempt owner or its designated evidence executor performs these recording
steps. Bounded workers return concrete evidence to the coordinator unless explicitly
assigned recording responsibility; do not create concurrent definitions or duplicate
assessments.

After task begin, inspect the existing task evidence with:
blackdog task outcome --project-root . --task TASK --json
Reuse an existing immutable definition and its hash, including when resuming a task
in a successor attempt. Record a definition before implementation only when none
exists; the definition belongs to the task, not each attempt. Define task-specific
observable outcomes and applicable compliance obligations separately. Include only
meaningful obligations;
for example, behavior preserved is an outcome, and required delegated ownership
respected is compliance. A changed objective requires new task scope, not rewriting
the definition after observing results.

For a missing definition, write UTF-8 JSON outside the repo and use the returned
task and current attempt IDs to record it with:
blackdog task outcome --project-root . --task TASK --attempt ATTEMPT --definition-file FILE --json

Definition format (replace example content with this task's actual contract):
{"schema_version":2,"task_class":"maintenance","objective":"Preserve behavior while simplifying the selected module","criteria":[{"id":"behavior","kind":"outcome","description":"Affected observable behavior is preserved","required":true},{"id":"ownership","kind":"compliance","description":"The assigned write ownership and delegation limits were respected","required":true}]}

Run configured validation from the task workspace using:
blackdog task validate --project-root . --task TASK --attempt ATTEMPT --run-id RUN --json
Choose a new stable run ID per intended invocation. Completed retries replay the
receipt; indeterminate runs do not prove execution or success. Preserve returned
event IDs and applicability. A machine command pass proves only that observation,
not delegation, independent review, acceptance or landing.

Before landing, assess each criterion from actual evidence using:
blackdog task outcome --project-root . --task TASK --attempt ATTEMPT --assessment-file FILE --json

Assessment format (use actual definition hash and unique assessment identity):
{"schema_version":2,"assessment_id":"review-behavior-1","definition_sha256":"ACTUAL_HASH","criterion_id":"behavior","result":"met","evaluator":"coordinator","evaluator_kind":"agent","provenance":"caller_declared","evidence_refs":[],"rationale":"Describe the concrete observed evidence and its limits","host_refs":[],"supersedes":null}

Results are met, not_met or not_assessed. Assessments remain caller-recorded;
reviewer_asserted does not authenticate an independent reviewer. evidence_refs can
reference only eligible machine validation event IDs from the same task, attempt
and source tree, not arbitrary transcript paths. Use rationale for concrete
observations and host_refs for actual host-owned transcript or worker locators.
These locators are caller declarations; Blackdog does not fetch or authenticate
them. Use no evidence_refs when the claim is supported only by inspected worker
evidence. Do not pretend validation proves compliance. A correction uses a new
assessment_id and supersedes the prior head.

Land through the existing lifecycle contract; evidence does not authorize landing
or replace repository guards. Read task outcome after landing, distinguish outcome
from compliance and landed status, and correct stale or incomplete assessments.
Missing assessment, timing, tokens, cost or host/model metadata stays unknown.
Do not invent zero interventions or cost estimates. Use Blackdog's measured timing
and host-owned usage evidence only within their coverage; overlapping phases cannot
be summed into total effort, and small samples do not prove an improvement.
