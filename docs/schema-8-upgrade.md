# Upgrade to schema 8

Schema 8 changes tool-call accounting. Older runtimes reject this database
version. Update the terminal command and every host plugin together before
resuming sessions. Updating a checkout alone does not update cached plugins.

Use one tested release for the whole upgrade. Tagged install commands require
that release to be published. For a local rehearsal, use a copied data folder
and install both the plugin and command from the same checkout.

## Prepare and rehearse

1. Record the current terminal and plugin versions and installed paths with
   `agent-efficiency doctor`. Record the data folder it reports. Keep the
   release or source commit needed to reinstall each old component.
2. Close agent sessions in every host. Stop status lines, hooks, and terminal
   commands that use this data folder. Keep them stopped through the backup,
   upgrade, and migration.
3. Copy the complete data folder to a new location outside the original folder
   and outside host plugin caches. Keep its permissions. Do not overwrite an
   earlier backup.

For example, replace these paths with the recorded data folder and two unused
backup locations:

```bash
ae_data=/absolute/path/from-doctor
ae_backup=/absolute/path/to/schema-7-backup
ae_trial=/absolute/path/to/migration-trial
test ! -e "$ae_backup" && cp -a -- "$ae_data" "$ae_backup"
test ! -e "$ae_trial" && cp -a -- "$ae_backup" "$ae_trial"
```

Copying the complete folder preserves database journals, settings, retained
packs, and vault registration. A database file copied while writers are active
is not an adequate backup.

Use the new command against the trial copy first:

```bash
agent-efficiency --data-dir "$ae_trial" migrate --check --json
agent-efficiency --data-dir "$ae_trial" migrate --apply --json
agent-efficiency --data-dir "$ae_trial" migrate --check --json
agent-efficiency --data-dir "$ae_trial" report --json
```

The first check must identify the old schema and target 8. After migration,
both schemas must be 8 and no migration must remain. Compare the retained
sessions, events, receipts, and activity totals with the backup. Old event
counts remain historical observations; this migration does not recalculate
them. The automatic schema backup supplements the complete folder backup.

## Update and verify every installed copy

Install the same release in the terminal, Claude Code, Codex, and Cursor where
they are in use. Follow the [host install steps](install.md). The installer
pins both Claude Code and the terminal command to its selected `--ref`.
For local development, `bash install.sh --source "$PWD"` uses one checkout
for both. An existing command used with `--no-command` must already match.

Verify each installed copy before allowing it to open the real data folder.
Use the paths reported by doctor. Its `agent_efficiency/store.py` must declare
`SCHEMA_VERSION = "8"`. Compare the installed code with the tested release
artifact or source commit. A version label alone is insufficient if an earlier
local build reused that label.

For Cursor, replace the plugin with a copy inside its local plugin folder.
An outside symlink is not loadable. For every host, reload the plugin or restart
the host so its active hooks use the new copy.

With writers still stopped, use the new command on the real folder:

```bash
agent-efficiency --data-dir "$ae_data" migrate --check --json
agent-efficiency --data-dir "$ae_data" migrate --apply --json
agent-efficiency --data-dir "$ae_data" doctor --json
```

Doctor must report the intended installed copies as ready. Run a new session
in each host in use and confirm that its events reach the intended data folder.
Packaged smoke tests do not establish that a live host loaded the new hooks.
Keep the backup until those checks and normal reporting pass.

## Roll back without losing the upgraded records

Stop every writer again. Preserve the complete schema 8 folder under a new
name. Restore the complete pre-upgrade snapshot to the original location, then
reinstall the old terminal command and all old host plugins before restarting
sessions. Check their installed paths and run doctor.

Do not point an old runtime at schema 8 or combine individual files from the
old and new folders. A restored snapshot excludes activity recorded after the
upgrade. Keep that activity in the archived schema 8 folder for later review.

## Start an experiment after the upgrade

Use the same tested runtime in every measured host. Keep failed and excluded
attempts with their reasons. Follow the task-outcome rules in
[Measurement](measurement.md). A completed migration, a passing smoke test,
or a lower cost per turn does not establish a lower cost per verified accepted
task.
