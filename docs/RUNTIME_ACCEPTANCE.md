# Portable Runtime Acceptance

`scripts/acceptance_runtime.py` runs public synthetic fixtures in temporary Git
repositories. It imports no Blackdog library. Every lifecycle operation runs in
a child process through the supplied artifact and then the installed runtime.
The harness reports timing observations only after every required assertion
passes. A failed scenario exits nonzero and leaves an existing report untouched.
Completed reports publish by atomic replacement.

Test a portable archive with one sample for CI or repeated samples for local
measurement:

```sh
python3 scripts/acceptance_runtime.py --artifact dist/blackdog.pyz \
  --samples 7 --output dist/runtime-acceptance.json
```

Measure an older committed source checkout with the identical lifecycle:

```sh
python3 scripts/acceptance_runtime.py --baseline-source ../blackdog-baseline \
  --samples 7 --output dist/runtime-baseline.json
```

The baseline mode records its Git revision, rejects uncommitted implementation
changes, and checks that the revision remains unchanged between samples.
Artifact mode reads one immutable byte snapshot before sampling and reports its
SHA-256. It initially runs with isolated Python and site packages disabled,
then deletes the original fixture archive after installation. All remaining
operations must run through the installed runtime without a source import path
or repository `.VE`. A temporary `python3` launcher pins shebang execution to
the harness interpreter, so the reported Python version matches the measured
runtime. This is dependency isolation, not a filesystem access sandbox.

Each fresh repository exercises installation, begin, show, recovery inspection,
close with a retained workspace, the exact emitted cleanup command and its
post-removal replay, a second begin, an actual content validation command,
canonical landing, and post-cleanup read surfaces. It checks target contents,
terminal task state, a clean target checkout, and absence of leaked worktrees.
Explicit Python handler and fault-injection cases belong to the runtime test
suite; this harness verifies the external artifact path independently.

The fixture sets the supported `BLACKDOG_HOME` override and omits provider
thread context. It does not read provider conversations or measure agent work.
Inputs are public synthetic text, and output JSON contains no fixture paths or
prompt bodies. Temporary fixtures are disposed after each sample.

Duration units are milliseconds measured with the monotonic clock. Every phase
reports eligible, observed, and missing counts; missing values never become
zero. The median uses the middle value or average of the two middle values;
p95 uses the nearest rank. Fewer than 20 samples carry a small-sample flag.
Scenario wall time includes fixture construction and verification and must not
be presented as the sum of measured phases. Compare the same scenario, Python,
platform, and validation conditions, and report per-command regressions as
well as setup or whole-scenario improvements.
