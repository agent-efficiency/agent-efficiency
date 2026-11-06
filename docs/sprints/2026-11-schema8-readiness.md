# Schema 8 readiness sprint

Started: 2026-11-06.

Goal: make the schema 8 release ready for a controlled installation across
Claude Code, Codex, Cursor, and the terminal command. Each readiness claim must
have retained evidence.

Work on `sprint/schema8-readiness`, based on `split/install-pins` at `6f8c775`.
The base includes the release fixes and upgrade guide. The vault experiment
remains on its separate review branch.

## Ordered work

| Item | Deliverable | Acceptance | Status |
| --- | --- | --- | --- |
| S8-1 | Report installed runtime schemas in `doctor`. | All three hosts fail readiness when an installed schema differs from the command or cannot be read. A matching version label does not hide a mismatch. Text and JSON identify the affected copy. | Complete |
| S8-2 | Keep a repeatable migration and rollback rehearsal in the repository. | Run with synthetic data in a new folder. Bind the result to the old and new source commits. Check retained records, backup integrity, old-runtime rejection, and rollback. Keep failed attempts. | Complete |
| S8-3 | Prepare the release evidence record and live-host procedure. | Record the tested commits, installed paths, schema versions, migration result, and one new session per active host. Mark unrun host checks as pending. | Prepared; live checks pending |

S8-1 reads the installed schema declaration without importing that runtime.
It preserves the existing rules for disabled plugins and project installs
that do not apply in the checked folder. Its checks must not migrate the data
folder. Matching schema declarations pass this check. They do not prove that
the host loaded the plugin or that the builds match.

S8-2 replaces the temporary rehearsal used during branch preparation with a
maintained command. It must not accept or mutate a live data folder.
Use `scripts/check_schema_upgrade.py` for the command. Resolve an explicit
baseline ref to a commit before exporting its plugin source. Record both
source commits and reject uncommitted candidate code. Use synthetic sessions,
events, and receipts. Each run gets a new output folder; an existing folder
must cause a refusal. The result must retain a failed step and its error,
not replace that attempt with a later pass.

S8-3 prepares a concrete release review. Publication and a live data migration
are separate actions. Their evidence remains pending until those actions run.

The owner requested a completed local build and a versioned candidate, with
publication held for approval. The local build passed, and the owner approved
publication of version 0.2.2 on 2026-11-06. The
[release notes](../releases/0.2.2.md) define its scope and pending checks.
Publication does not complete the live checks in S8-3.

## Completion evidence

- Tests cover an old schema, a newer schema, an unreadable schema, matching
  schemas, and matching package labels with different schemas.
- Existing host enablement and folder-scoping tests still pass.
- The full test suite, pre-commit checks, pack builders, distribution check,
  wheel installation, and isolated doctor pass. Run available host validators.
- Python 3.11 runs the new tests.
- The migration rehearsal retains its inputs, output, and result.
- Every release record distinguishes local fixture checks from live-host
  evidence.

## Later work

Join vault assignments to verified task outcomes before evaluating cost per
accepted task. Keep failed and abandoned task costs in the comparison. That
work does not block these release fixes and cannot be replaced by a lower
cost per turn.

## Progress

- Confirmed that local `main` matches freshly fetched `origin/main` at
  `e954a22`.
- Created the sprint branch from the release fixes. Existing worktrees and
  the combined experiment branch remain available for review.
- Completed S8-1. Doctor reports the command's schema and the schema declared
  by each installed copy. Unknown or different schemas prevent the affected
  host from being ready and skip its hook probe.
- The 764-test release-branch suite passed. All 9 new tests passed on Python
  3.11. The focused doctor run passed 15 tests. Pre-commit, pack builders,
  distribution checks, wheel installation, isolated doctor, and Claude plugin
  validation passed. Claude reported an optional listing-icon warning.
- The regression tests confirmed that schema inspection leaves an existing
  schema 7 database unchanged. No live installation or migration was run.
- Implemented S8-2 as `scripts/check_schema_upgrade.py`. It retains source
  archives, step logs, synthetic data, and a result for each attempt. All 13
  focused tests passed on the default Python and Python 3.11.
- The rehearsal passed against the retained schema 7 source. The first
  attempt failed on temporary SQLite coordination files and remains retained.
  The corrected comparison includes logical database records and persistent
  files. Backup comparison still includes every copied file.
- Prepared [Release review](../schema-8-release-review.md) for S8-3. It names
  the required evidence and the live-host procedure. Publication, installed
  host checks, and live migration remain pending.
- The 777-test suite passed after S8-2. Pack builders, distribution checks,
  wheel installation, isolated doctor, pre-commit, and Claude plugin
  validation also passed. The release review still requires live evidence.
