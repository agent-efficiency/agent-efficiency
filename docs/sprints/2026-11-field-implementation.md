# Field-opportunity implementation sequence

This work implements the [field backlog](2026-11-pi-field-opportunities.md) in
small validated slices. The backlog is not complete merely because a CLI or a
design exists. Host integration, negative controls and documentation are part of
acceptance. No new bundled guidance is enabled by this work.

## Sequence and status

1. **PF-1, prerequisite planning — implemented and locally validated.**
   `check --plan` is read-only, checks transitive receipt freshness, rejects invalid
   graphs and preserves legacy configuration digests. It does not enforce a plan,
   execute dependencies, or approve a target result. All 915 Python and seven Node
   tests passed at the implementation commit, including packaging/host checks.
2. **PF-3, environment identity — implemented; focused tests pass; migration review pending.**
   Explicit allowlisted environment names contribute keyed digests to check
   identity. An unusable private key makes verification inconclusive, not a public
   hash of secrets. Missing and empty values differ. No values enter telemetry.
   A context selector such as HOME identifies a location, not the contents of its
   installed packages; configured preflight checks remain necessary for versions.
3. **PF-2/PF-7, asynchronous lifecycle — managed CLI implemented; host integration pending.**
   A launch creates pending activity, never passing verification. Completion must
   be correlated with the check, launch workspace/configuration/environment and
   task identity. Missing, killed, expired or conflicting terminal evidence is
   unknown/blocked/inconclusive, not success. Duplicate events must be idempotent.
   A repeated start must point to existing activity rather than launch another
   command. Cross-process writes require transactions; terminal receipts and their
   identity must survive restart. Host event limitations must remain explicit.
4. **PF-4, structured result interpretation — implemented; focused tests pass.**
   Project-configured parsers accept a bounded closed result schema. Baseline,
   collection/setup, survivor and timeout outcomes remain distinct; arbitrary
   output or nonzero exit is not proof of a caught mutation. No raw output stored.
5. **PF-5/PF-8, reuse and plan continuity — implemented; focused tests pass.**
   Explain why evidence is current/stale without changing freshness semantics.
   Use declared dependencies and explicit project-owned milestones, not private
   prompt extraction. An artifact comparison is an explicit configured check,
   never a heuristic permission to skip tests. Persist metadata/digests only.
6. **PF-6/PF-9, coverage and rework reports — implemented; focused host/cohort fixtures pass.**
   Report supported versus unavailable lifecycle signals, separating launches,
   terminal results, previews, interventions and acceptance. Add fixed retry-cause
   codes and rated corrections to existing measurement; retain failed runs and
   separate estimates from charges. No savings claim without matched evidence.

## Current validation boundary

Development remains isolated from the active Pi source loader and database.
The first full run executed 946 tests and failed only two stale schema-8 assertions;
the updated assertions retain event-preservation checks and target the current
schema. Its full retry passed all 946 tests. Subsequent cancellation, Pi coverage,
and rated-cohort changes passed final validation: **960 Python tests, 7 Pi Node
tests**, both pack checks, distribution validation, real wheel installation, and
all pre-commit checks. Migration/rollback rehearsal against committed source and
final acceptance review remain before closing the field backlog.

Seven real-worker/negative controls now pass: detached completion, launch failure,
private explicit output, no output truncation, timeout, atomic receipt/job rollback,
and avoiding a signal to a reaped child's old process group. The first worker test
run incorrectly mocked Git subprocesses as well as workers; that failed run is
retained, and the corrected controls pass. All seven existing Pi Node tests and
both generated pack checks passed. None of this is landing or migration approval.

Pi replay fixtures now explicitly report external background completion and
acceptance coverage as unavailable; tool success cannot create a verification
receipt. Real CLI and milestone-order controls pass. Rated rework supports fixed
phases and same-project session links; existing experiment cohorts include these
annotations even for unaccepted work, without adding potentially overlapping costs
to the original comparison metrics. Workflow and experiment regressions pass.

Remaining acceptance work includes migration/distribution review and final complete
gates. External host completion interception is intentionally unsupported, not
implied by managed CLI support. Do not label all nine opportunities complete until
the reviewed acceptance matrix and remaining gates establish their actual scope.

## Gates for each slice

Write negative tests first, preserve the failed pre-implementation run, implement,
run focused regressions, then run the repository's complete validation before
committing. Preserve existing receipt and schema compatibility or explicitly
review the migration. Run privacy tests with planted sensitive values. Keep
runtime standard-library-only and hooks bounded, deterministic and fail-open.

Before any Pi harness integration, read the relevant installed Pi lifecycle/API
documentation and examples, and add deterministic host fixtures. No reload while
tracked project jobs run. Generic CLI capabilities do not imply automatic support
for every host's custom background tools.
