# Schema 9 coordinated upgrade

Version 0.3.0 adds closed environment/structured-result reasons and metadata-only
managed-job, result-summary and workflow-annotation tables. Publication does not
upgrade an active installation. Follow this coordinated procedure before changing
any existing command, plugin or source loader. Synthetic schema-7 and schema-8
migration/rollback rehearsals passed; live installation verification remains a
separate requirement.

## Before installing

1. Inventory every command, plugin and host loader writing the data folder. Pin
   all of them to the same reviewed build; a local source loader counts too.
2. Finish or deliberately stop tracked jobs before stopping their hosts. Do not
   reload an active host merely to pick up these changes.
3. Stop every writer and preserve the **whole** private data folder, including the
   database, key material and configuration. Protect it with the same permissions
   as the original. Do not copy a live database file or publish these artifacts.
4. Rehearse the upgrade and rollback against synthetic/private copies, never the
   live host database. The schema-8 receipt rebuild must preserve IDs, values and
   existing experiment links. Schema-7 migration coverage remains required too.

## Upgrade

Install the reviewed command/plugin/loader versions together. On first use, the
new runtime coordinates its migration using the bounded folder lock and retains
a private `.schema-8.bak` database backup before changing the receipt constraint.
Confirm `doctor`, runtime schema, configured mode and supported host capabilities
before resuming sessions. Check that old receipts remain visible and that new
closed reason codes work. Checks without optional new settings keep their original
check/configuration identity.

The automatic database backup is a recovery aid, **not** a substitute for the
stopped-writer full-folder backup. Optional tables are created on first explicit
use. Environment values, commands, output, arbitrary test labels and prompt text
are not copied into the new metadata tables.

## Rollback

Stop every writer again. Archive the entire upgraded folder separately so new
activity is not silently discarded. Restore the complete pre-upgrade folder and
its matching old runtimes. Do not point an old runtime at schema 9, mix individual
files across backups, or delete recent activity to produce a cleaner comparison.

Managed-job state is durable metadata, not an OS process handle. Restoring a data
folder does not stop or resume a worker. Resolve live processes using their trusted
host/process handles before rollback; never kill a process by an unverified old PID.
