# Field workflow acceptance matrix

Runtime implementation: `8e2bb10` on the isolated `feat/field-workflow` branch.
Status: local implementation accepted within the explicit CLI/observe-only scope
below. This is not rollout approval or a savings claim. The active Pi loader remains
unchanged; external host task interception is not implemented.

| Opportunity | Implemented contract | Acceptance evidence and boundary |
| --- | --- | --- |
| PF-1 prerequisites | Explicit transitive dependency plan and current-receipt checks | Graph, missing/stale/failed prerequisite and ready controls in `test_prerequisite_plan.py`. Advisory: no inferred release approval or automatic execution. |
| PF-2 asynchronous checks | Durable managed-job identity, atomic receipt completion, deduplication and cancellation precedence | `test_verification_jobs.py` and real-worker `test_background_verification.py`: launch is not pass; failed/changed/unknown outcomes cannot become success; restart and duplicate/out-of-order completion controls. External host task completion is not intercepted. |
| PF-3 environments | Opt-in private keyed environment selectors | `test_verification_environment.py` and `test_environment_schema.py`: changed/missing/empty selectors, private-key failure, privacy, receipt preservation. Paths identify selectors, not installed package contents; explicit dependency checks remain necessary. |
| PF-4 result classification | Closed bounded summaries distinguish assertions, baseline/setup failures, survivors, timeout and incomplete scope | `test_structured_results.py`: malformed/stale files refused; all-pass summaries cannot override nonzero exits; only fixed counts persist. No arbitrary-output guessing. |
| PF-5 evidence reuse | Explicit reuse of current immutable evidence with current prerequisites | `test_workflow_reporting.py`, `test_workflow_cli.py`, `test_retained_artifact_check.py`: source/artifact change reruns the check; retained comparison proves only its configured scope. Ignored/external artifacts are outside workspace binding and must not be assumed unchanged. |
| PF-6 coverage | Pi and CLI expose supported versus unavailable evidence paths | `test_pi_coverage.py` replays shell and custom-background launches; neither becomes verified acceptance or a receipt. Existing Pi Python/Node controls retain observe-only, privacy and fail-open behavior. |
| PF-7 job handling | Reuse active managed tasks; point-in-time inspection; no automatic polling or expired-task relaunch | Lifecycle and real-worker controls cover startup failure, explicit private output, non-truncation, timeout and safe cancellation. Host notifications/cancellation require that host's tracked runner; there is no unsafe stored-PID kill command. |
| PF-8 continuity | Ordered project-owned milestones reconstructed from current receipts | CLI/report tests retain pending, failed, blocked and ready distinctions. Later milestones cannot bypass earlier ones. Configuration changes invalidate evidence; no prompt-derived plan, percentage or stage approval. |
| PF-9 rework | Fixed retry/correction causes, ratings/phases, explicit same-project session links and cohort annotations | `test_workflow_experiments.py` and existing experiment tests: unaccepted work retained; estimates separate from reported costs; unknown overlap prevents adding annotation costs to comparison totals. Existing acceptance/matching gates are unchanged. |

## Validation record

- Final runtime suite: **960 Python tests**, **7 Pi Node tests**.
- Both generated pack checks, distribution validation, real wheel installation,
  Ruff and all pre-commit checks passed.
- Real schema-7 and schema-8 source baselines both migrated to the committed
  schema-9 runtime and rolled back successfully using synthetic folders only.
  Checks include exact historical values/IDs, full backups, idempotency, old-runtime
  rejection, post-upgrade activity and unchanged rollback/archive evidence.
- The expanded rehearsal runner passed all **14** focused tests. The additional
  workspace-bound retained-artifact acceptance test passed independently.
- Final tooling regression: **961 Python tests** passed; the retained-artifact
  acceptance control passed separately (it was added after full-suite discovery).
- Isolated-home doctor, strict Claude plugin validation, all pre-commit checks and
  distribution validation passed. The plugin validator retained its nonblocking
  missing-icon warning; a Python SQLite ResourceWarning was also retained in logs.
- Logs, synthetic migration artifacts and failed attempts are retained privately,
  not published. No active installation was migrated, reloaded or upgraded.

## Deployment and claim limits

Follow [the coordinated schema-9 procedure](../schema-9-upgrade.md) only after
review and installation approval. Do not reload hosts while tracked jobs run.
None of these tests establishes live native-host lifecycle integration, billing
savings, intervention usefulness or release readiness. Unsupported signals stay
unavailable, and no new bundled advice or guard enforcement is enabled.
