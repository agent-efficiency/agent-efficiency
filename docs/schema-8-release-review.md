# Schema 8 release review

Review the release fixes separately from the vault experiment. A passing
synthetic rehearsal supports migration review. Live installation and session
evidence are separate requirements.

Version 0.2.2 is held for owner approval. Read its
[release notes](releases/0.2.2.md) with the evidence record. A local version
tag and built artifacts do not authorize publication or a live upgrade.

## Run the maintained rehearsal

From the checkout being reviewed:

```bash
ae_checks=$(mktemp -d)
python scripts/check_schema_upgrade.py \
  --baseline-ref 3aec46e34ab2603a45926f9be0a3df7464a9fe03 \
  --artifacts "$ae_checks/schema8"
```

The baseline above is the retained schema 7 source commit. The command resolves
that ref and the candidate's `HEAD` to full commit IDs before exporting their
plugin sources. It uses local Git objects and performs no fetch. The candidate
plugin must have no uncommitted tracked or untracked files. Maintainer scripts
and documentation are outside that runtime check. The result also records the
hash of the rehearsal script that ran.

The output folder must be new, outside the checkout, and have an existing
parent. Existing files, folders, and links are refused. The command has no
input for a live data folder. It executes the two source exports with a new
home folder and synthetic sessions, events, vault receipts, and verification
receipts.

The rehearsal checks:

1. The baseline creates schema 7 without the schema 8 event column.
2. The complete folder is copied to a backup and an upgrade copy.
3. The migration check identifies the expected versions and leaves records
   and persistent files unchanged.
4. Migration preserves every value in the original table columns, including
   historical activity counts. It adds the event column with its expected
   default and creates a valid automatic schema backup.
5. Applying migration again preserves records and backups.
6. The old runtime rejects schema 8 for the expected reason.
7. The new runtime records more synthetic activity after migration.
8. Restoring the original backup lets the old runtime open the original
   records. The backup and the upgraded copy remain intact.

SQLite can create or remove temporary coordination files during reads.
Active database comparisons include logical records, including records in
the write-ahead log, and persistent file hashes. The untouched backup is
compared byte for byte, including any journal files copied into it.

## Read and retain the result

Read `result.json` in the output folder. Only `result: passed` with `ok: true`
is a completed pass. A timeout or interruption is incomplete. If the process
is killed before it can finish the receipt, its last saved result can remain
`running`; that is not a pass. A failed final receipt write cannot produce a
successful command result.

The folder retains source archives and hashes, synthetic data copies, step
results, stdout, and stderr. A failed or incomplete attempt stays in its own
folder. Run a later attempt in a new folder and retain both. The exit status
is 0 for a pass, 1 for a failed or incomplete attempt, and 2 for refused
arguments or an output folder that cannot be created.

The timeout defaults to 30 seconds per subprocess. Set `--timeout SECONDS`
before a run if the environment needs a different limit. The receipt records
that limit. A later attempt does not replace an earlier incomplete result.

## Evidence record

Copy these fields into the release review after each applicable check. Keep
unrun checks pending. Link the actual artifacts rather than only recording
that a command passed.

| Evidence | Record | Required result |
| --- | --- | --- |
| Candidate | Full commit ID and plugin archive hash from `result.json` | The reviewed source is the source being installed. |
| Baseline | Full commit ID and plugin archive hash | Schema 7 source is retained for rollback. |
| Local validation | Test counts, Python versions, logs, distribution and wheel checks | Required checks pass for the reviewed candidate. |
| Rehearsal | Result path, script hash, step results, failed or incomplete attempts | A completed pass with retained records and unchanged backup. |
| Terminal command | Installed path, version, runtime schema, installation artifact | Matches the tested release and reports schema 8. |
| Claude Code | Installed path, version, schema, enablement, new-session evidence | Active hooks record events in the intended data folder. |
| Codex | Installed path, version, schema, hook approval, new-session evidence | Active hooks record events in the intended data folder. |
| Cursor | Installed path, version, schema, plugin reload, new-session evidence | Active hooks record events in the intended data folder. |
| Live data | Original folder, complete backup location, migration result | Recorded history survives the real migration. |
| Rollback | Retained old installers and backup, location for upgraded records | The restore procedure can preserve both histories. |

For a host that is not used, record `not used` with that scope explicitly.
For a host that is used but has not been exercised, record `pending`.
Packaged smoke tests and matching schema declarations do not prove that a
host loaded its installed hooks.

## Live-host procedure

1. Review the candidate and local evidence. Keep the vault experiment opt-in
   and outside this release decision.
2. Follow [Upgrade to schema 8](schema-8-upgrade.md) to stop writers, keep a
   complete backup, update every installed copy, and migrate the data.
3. Run doctor from the new terminal command. Record installed paths and
   schemas. Resolve mismatches before restarting agent sessions.
4. Start a new session in each active host. Use a fixture project for a file
   read and an intentionally failing command. Confirm that the intended data
   folder records the host, one tool call per invocation, and the failure.
   Retain the result without prompt or command bodies.
5. Confirm that earlier sessions and receipts remain reportable. Keep the
   pre-upgrade backup and old installers until the review is accepted.

Publication, real installation, and live data migration remain pending until
performed. This record does not establish cost savings or task acceptance
improvements.
