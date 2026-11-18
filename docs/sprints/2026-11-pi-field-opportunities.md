# Pi field opportunities: cost to a verified result

Original status: proposed backlog, not demonstrated savings. The subsequent
[implementation acceptance matrix](2026-11-field-acceptance.md) records validated
CLI/observe-only capabilities, unsupported host paths and rollout limits. No new
bundled advice is enabled.

These opportunities came from one ongoing coding session using the experimental
Pi observe-only adapter. The evidence combines local telemetry inspection with a
manually maintained build/rework ledger. No matched native-terminal cost baseline
exists. The session includes tooling work as well as product work; it is not a
clean experiment. Private project files and transcripts are not included here.

## What was actually observed

At one inspection boundary the adapter reported 304 tool calls, 15 tool-reported
failures, and seven `core.verify-change` previews. These are different quantities:
a tool error is not a test failure, and a preview is not delivered advice. The
adapter reported zero repeated-action and broad-scan signals, but the ledger still
contained avoidable reruns. Those counters are not measures of all rework.

The ledger documented:

- A push before the separate sanity gate, followed by remote formatting failures.
- A full mutation sweep started before its reference-dependent controls could pass.
- Test failures caused by the wrong collection environment, inherited signal
  handling, ambient inventory and bytecode cached under an old source path.
- Background launches that succeeded while their eventual checks failed.
- Mutation results with distinct causes: failed baselines, missing test nodes,
  surviving mutations, and setup errors that did not exercise the target assertion.
- Successful reuse of retained recordings, after checking the exact reference delta
  and source binding, instead of regenerating unchanged expensive evidence.
- Foreground commands that exceeded their timeout and then had to be relaunched as
  background jobs; a background wrapper also rejected a complex inline command.
- A user correction to return to the declared build-plan order.

These are observed workflow problems and candidate improvements. We have not shown
that the adapter detected, prevented or caused any of them.

## Proposed backlog

| ID | Priority | Opportunity | Proposed behavior | Acceptance evidence |
| --- | --- | --- | --- | --- |
| PF-1 | First | Check prerequisites before expensive gates or publication | Project-declared check dependencies and action prerequisites. Explain the exact missing/stale prerequisite before a configured expensive action. Do not infer readiness from test names or a partial pass. | Synthetic dependency fixtures cover a stale reference, missing sanity, failed prerequisites, cycles and an already-ready action. Only the relevant advisory fires; no automatic execution or blocking without consent. |
| PF-2 | First | Track asynchronous checks through terminal completion | Correlate launch, task identity and terminal result with the configured check and launch-time workspace/configuration digest. Distinguish started/running/pass/fail/killed/timed-out/unknown. | A successful launch followed by exit 1 never creates a passing receipt. Duplicate terminal events are idempotent; missing events remain unknown; changed files make completion evidence stale/inconclusive. Test interruption, resume and out-of-order events. |
| PF-3 | Next | Make environment compatibility explicit | Extend configured verification identity with a privacy-safe environment/capability receipt. Offer bounded, explicit preflight checks for declared dependencies before a long gate. Build on existing workspace/configuration receipts rather than replacing them. | Two environments at the same revision cannot silently share a receipt when declared dependencies differ. Missing collections/tools and stale tool versions are reported before the expensive command. No raw environment values, home paths, inventory or command output stored. |
| PF-4 | Next | Distinguish failed verification from an invalid probe | Support opt-in structured result summaries: baseline failure, missing target, assertion reached, survivor, setup/collection error and timeout. Keep the command exit status but do not turn every nonzero mutation exit into a caught mutation. | Synthetic results cover each class, including a fixture that fails before the target assertion. Unknown formats are inconclusive, never guessed into passes. Reports preserve partial coverage and require the declared full gate. |
| PF-5 | Next | Reuse valid evidence without weakening the gate | Make existing receipt validity and invalidation reasons visible; investigate a project-defined retained-artifact comparison check for reference-only changes. Reuse must be an explicit supported operation, not a generic “skip tests” recommendation. | Identical retained input plus current source/configuration binding can satisfy only the configured comparison check. Changed source, environment, artifact digest or required scope invalidates reuse. AST equality alone cannot establish runtime equivalence. Original receipts remain immutable. |
| PF-6 | Next | Measure useful telemetry coverage, not just counters | Publish capability/coverage states for custom tools and asynchronous results. Separate tool-launch errors, terminal verification outcomes, previews, delivered interventions and verified acceptance. Label unavailable signals rather than presenting them as zero. | Replay a session containing synchronous checks and custom background tools. Unsupported paths are visible and cannot inflate validation counts or completion evidence. No transcript backfill or raw-result storage. |
| PF-7 | Later | Avoid unnecessary long-job reruns and polling | Use declared task lifecycle/capabilities to recommend a tracked runner for known long checks; point to existing running tasks instead of polling or relaunching. Surface known host lifecycle limitations, without assuming all hosts preserve jobs on reload. | A running task is not relaunched; an explicit user status request remains allowed; completion notifications end waiting. Missing events do not trigger unbounded polling. Saved-command execution retains original exit codes and full output outside telemetry. |
| PF-8 | Later | Preserve plan order and evidence state across handoffs | Explore a project-owned structured milestone/check manifest, with current prerequisites and immutable receipts. Report divergence from an explicitly supplied plan; never extract or persist a plan from private prompts. | Resume fixtures retain “pending” versus “failed” versus “passed” accurately. An approved plan change is respected. No completion percentage or stage approval is invented from tool counts. |
| PF-9 | Measurement | Account for rework and user corrections | Extend the existing matched-task experiment/reporting approach with explicit retry causes and user-rated workflow corrections. Report setup/tooling overhead both separately and in all-in cost. | Failed work remains in cohort accounting. Launch/runtime/review/idle overlap are not double-counted. Estimated prices are distinct from charges, and unmatched host/model/environment comparisons cannot produce a savings claim. |

## Design constraints and sequencing

1. Specify PF-1/PF-2 contracts and deterministic fixtures first. Their initial
   deployment should be observe-only with explicit coverage reporting.
2. PF-1 depends on trustworthy state from PF-2/PF-3 where checks run asynchronously
   or across environments. PF-4 adds result meaning; a launch exit code is not a
   substitute for either lifecycle or semantic evidence.
3. Reuse the existing verification, freshness, consent and experiment machinery.
   Do not introduce a parallel definition of “current” or “accepted.” These items
   are integration gaps/hypotheses, not a claim that receipts do not already exist.
4. Lifecycle hooks remain local, deterministic, bounded and fail-open. Dependency
   probes and artifact review belong in explicit commands, not slow hook work.
5. Persist only approved metadata such as opaque IDs, fixed result/reason codes,
   counts, timestamps and keyed digests. Hashes do not by themselves anonymize
   low-entropy sensitive environment values. Use allowlisted capability/version
   classes or keyed fingerprints; never store arbitrary environment dumps.
6. Do not bundle new guidance simply because this session suggests it. Follow
   the existing usefulness-rating and pilot gates in [measurement](../measurement.md).
   Observe-only previews must remain distinct from delivered interventions.
7. Keep all existing build/review/security gates. Repeated work may be required
   after a real source change; the goal is less avoidable rework, not fewer checks.

## How we would learn whether these help

For each prospective intervention, record the eligible opportunity, what the
system could actually observe at that point, the proposed action, and a human
rating. Explicitly count false alarms such as a queued valid check, an approved
plan change, or validation that ran through an unsupported tool.

Use the existing matched-task pilot, acceptance and cost-claim rules. Report
prerequisite-related wasted runs, time to terminal evidence, stale-evidence
acceptance attempts, reviewer effort, correction turns and all-in cost separately.
Do not infer prevented failures or saved tokens from a preview count.

The immediate deliverable is this backlog. Implementation needs separate designs,
tests and review, including host capability/semantic alignment. It must not interrupt
an active project gate or require a host reload while tracked jobs are running.
