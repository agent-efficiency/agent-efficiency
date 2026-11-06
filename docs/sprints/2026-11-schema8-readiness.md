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
| S8-1 | Report installed runtime schemas in `doctor`. | All three hosts fail readiness when an installed schema differs from the command or cannot be read. A matching version label does not hide a mismatch. Text and JSON identify the affected copy. | In progress |
| S8-2 | Keep a repeatable migration and rollback rehearsal in the repository. | Run with synthetic data in a new folder. Bind the result to the old and new source commits. Check retained records, backup integrity, old-runtime rejection, and rollback. Keep failed attempts. | Queued |
| S8-3 | Prepare the release evidence record and live-host procedure. | Record the tested commits, installed paths, schema versions, migration result, and one new session per active host. Mark unrun host checks as pending. | Queued |

S8-1 reads the installed schema declaration without importing that runtime.
It preserves the existing rules for disabled plugins and project installs
that do not apply in the checked folder. Its checks must not migrate the data
folder. Matching schemas establish compatibility for this upgrade check;
they do not prove that the host loaded the plugin or that the builds match.

S8-2 replaces the temporary rehearsal used during branch preparation with a
maintained command. It must not accept or mutate a live data folder.

S8-3 prepares a concrete release review. Publication and a live data migration
are separate actions. Their evidence remains pending until those actions run.

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
- Started S8-1 with regression tests for installed schema mismatches.
