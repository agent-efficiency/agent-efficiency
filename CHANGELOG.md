# Changelog

## Unreleased

### Fixed

- Keep guard mode on when the project config changes, breaks, or is removed
  during a session. Before, a config that failed to load turned guard into
  advise, and a changed config was accepted without asking. Now a session
  pins the config the first time it runs in guard. If the file later differs
  from the pinned one, is not a valid config, or is not in the working
  directory, guard stays on, and when the agent stops after a code change it
  is asked once per turn to tell you. Only you can accept a changed file, by
  typing `$agent-efficiency guard` (or `$agent-efficiency advise` when the
  file no longer sets guard) in the session. While the file still sets guard,
  or is not a valid config, `$agent-efficiency advise` and `on` keep guard on
  and say why, and `observe` and `off` turn guard off but keep the pin. A
  config file that exists but does not load also turns guard on in an
  `advise` session.
- Treat a project config larger than 256 KiB, one that is not a regular file
  (such as a named pipe), or one nested too deeply to parse as not valid. The
  file is opened without waiting, so such a file can no longer stall or crash
  a hook.
- Count a shell or subagent call that repeats a command with a new
  description as a repeat. The free-text description is no longer part of the
  call fingerprint for shell tools and the subagent tools `Task`, `Agent`,
  `spawn_agent`, and `subagent`. Other tools keep it.

### Added

- Action fingerprints are keyed with a private key in the data folder,
  `digest.key`, created with mode 0600 on first use and never replaced.
  Fingerprints stored before this release keep their old form. Where the
  system has no user ids, the key file's owner and mode are not checked.
- `doctor` reports the digest key and fails when the key file is not valid. It
  says when the key cannot be created because the data folder is not
  writable. It never prints the key.
- `doctor` names each Claude Code setting that sends hook or status line
  writes to a data folder other than its own, with the setting to change, and
  fails while the store is split. It reads `env.AGENT_EFFICIENCY_DATA`, and
  `--data-dir` or an `AGENT_EFFICIENCY_DATA=` prefix in `statusLine.command`,
  from the `CLAUDE_CONFIG_DIR` folder, `~/.claude`, the project settings, and
  each folder given with the new `--host-config` option. In
  `statusLine.command` it reads only the parts that run this tool, and an
  earlier part that assigns or exports the data folder. A setting it cannot
  resolve is reported as not checked. It never edits a settings file. Codex
  and Cursor are reported as not checked.
- `doctor` reports whether `git merge`, `gh`, and `agent-efficiency` need a
  permission prompt in Claude Code for the folder it checks: allow, ask, deny,
  or not set, with the rules and files that decide it. A rule whose command
  holds a tab, a newline, or a run of spaces is listed as not interpreted.
  This report does not change the doctor result.
- `doctor` treats settings files as untrusted. A file it cannot parse,
  including one nested too deeply, is reported as not read. A trust file
  (`.claude.json`) that is not a regular file of at most 64 MiB counts as no
  saved trust. In the text output of the settings and permission reports,
  each settings value is shown on one line, with control characters escaped,
  and cut to 200 characters.

### Known limits

- Accepting a changed config lasts for the current session. A new session pins
  the file it finds.
- Guard reads the project config only from the working directory of each
  event, not from parent folders. After a session pins the config, when the
  agent works in a subfolder or another project, guard stays on and reports
  the file as not found or different.
- Terminal mode commands and `agent-efficiency experiment enroll` can also
  change the pin. `agent-efficiency mode guard` pins the current file, and
  the other mode commands and enrollment clear the pin or turn guard off. An
  agent with shell access can run them.
- The permission report matches only the bare `agent-efficiency` command
  name. A rule that allows an interpreter or a full path, such as
  `Bash(python3:*)`, can also run the tool and is not evaluated; the report
  says so.

## 0.2.2 - 2026-11-06

See the [release notes](docs/releases/0.2.2.md) and the schema 8 upgrade
requirements below.

### Added

- A repeatable schema 8 migration and rollback rehearsal using committed
  sources and synthetic data. Each attempt retains its own artifacts and
  result, including failures and incomplete runs.
- Task guides for installation, verification, vault context, reports, and
  development.
- A Claude Code installer that selects the same release for the plugin and
  terminal command and preserves settings permissions during updates.

### Upgrade

Schema 8 requires a coordinated update of the terminal command and host
plugins. Follow the [upgrade and rollback steps](docs/schema-8-upgrade.md).
Keep a complete backup and verify the installed copies before restarting
sessions. Older runtimes cannot open the upgraded database.

### Fixed

- Report installed plugin schemas in doctor. An affected host is not ready
  when its schema differs from the terminal command or cannot be read, even
  when the package version labels match.
- Report data entries with shared permissions and Cursor plugins that cannot
  load from an outside link.
- Deliver vault context after compaction through an event that accepts it.
- Count each tool call once when a host reports it more than once. Claude Code
  reports a call before it runs, after it finishes, and again in a batch; the
  repeated-action and failure-loop guidance now fire on real repeats only.
  Older records keep their previous counts.
- Keep separate outcomes for identical calls whose result reports arrive in
  different orders. Use a digest of the host call identifier when supplied;
  otherwise match successful and failed reports separately.

## 0.2.1 - 2026-09-30

### One data folder for every host

Hooks under Claude Code and Codex kept their data in the host's plugin data
folder, while the terminal command used `~/.local/share/agent-efficiency`. So
`default`, `mode`, `vault register`, `status`, and `report` in a terminal never
reached or saw a real session. The data folder is now the same for every host
and the terminal command: `--data-dir` or `AGENT_EFFICIENCY_DATA` when set,
else `$XDG_DATA_HOME/agent-efficiency`, else `~/.local/share/agent-efficiency`.

What to do: nothing, in most cases. The first hook or terminal command that
finds no database in the data folder copies an existing plugin data store into
it once: the database, `vault.json`, and the guidance files. Until that copy
is done, nothing starts a new store in its place, and a plugin data folder
that cannot be read counts as one that may hold history. A hook that cannot
finish the copy within its time limit records nothing for that event and the
next event tries again, and any terminal command, such as
`agent-efficiency doctor`, finishes it or says why it cannot. A folder you chose with `--data-dir` or
`AGENT_EFFICIENCY_DATA` never receives a copy. The old folder is left in
place, and `doctor` lists it as copied or not copied by comparing the two
stores. If the data folder already had its own database, nothing is copied,
and doctor lists the plugin folder with steps that copy it while keeping both
histories. If you used
both Claude Code and Codex, only one store is copied, and doctor lists the
other. Your data also no longer lives in the folder that
`claude plugin uninstall` deletes.

### Install the command with pipx or pip

A plain `pipx install` or `pip install` of the repository installed a command
that failed in `doctor`, `knowledge status`, and `smoke-test`, because the
bundled policy and guidance packs were not part of the package. They are now.
What to do: install the command with
`pipx install git+https://github.com/agent-efficiency/agent-efficiency@v0.2.1`.
Such an install has no plugin folder, so `doctor` says there are no plugin
files to check, and `smoke-test` runs the installed package's hook entry
point.

### Added: doctor checks the current folder

A Claude Code install can be present and still not run in a folder. Claude
Code loads the plugin when the last settings file that names it turns it on.
Those files are, in order: the user settings; `.claude/settings.json` in the
folder the session starts in; `.claude/settings.local.json` in that folder;
`.claude/settings.local.json` at the root of the main checkout of the git
repository; and the managed settings files. `doctor` now reports which install
applies in the current folder, or the one `--cwd` names, which file enables or
disables it, and whether workspace trust is saved. An install that is disabled
or not enabled there fails the check. A broken install made for another folder
is shown as a warning. Managed settings delivered by a server or by device
management are named as not checked. What to do: run the command `doctor`
prints, such as
`claude plugin enable agent-efficiency@agent-efficiency --scope project`, or
`claude plugin uninstall agent-efficiency@agent-efficiency --scope project
--keep-data` to let the user install apply.

### Fixed

- `agent-efficiency doctor` without `--json` crashed with a `KeyError`. It now
  prints its report.
- `doctor` reported every host as ready even when nothing was installed,
  because it checked only its own package files. It now also checks the copy
  each host installed, from the Claude Code and Codex install records and the
  local Cursor plugin folder, including a stale `hooks/hooks.json` there, and
  starts each installed copy's hook command once to prove it can run. A host that is not installed is reported as not
  installed.
- A passing Python check was recorded as `inconclusive`, because the test run
  wrote `__pycache__` into the workspace. Checks now run with
  `PYTHONDONTWRITEBYTECODE=1`, and untracked files in `__pycache__`,
  `.pytest_cache`, `.mypy_cache`, and `.ruff_cache` folders no longer count
  as workspace changes. A check that changes other files, or leaves a file or
  symlink with one of those names, or a symlink inside one that points out of
  the workspace, is still not a pass.
- A result that is not a pass now records a reason, and `check`,
  `evidence list`, and `evidence show` print it in one sentence. Receipts from
  earlier versions show that no reason was recorded.
- `vault check` and the vault git hooks passed when `index.json` no longer
  matched the notes, so a new note never reached a session. They now fail with
  `index_stale`. What to do: run `agent-efficiency vault index TREE`, then
  commit `index.json` and `INDEX.md`.
- The vault git hooks failed with `exec: agent-efficiency: not found` when only
  the plugin was installed. They still stop the commit or push, and now say
  why and how to install the command. What to do: run `agent-efficiency vault
  init TREE --classification CLASS` again on each tree to update its hooks.
- The data folder and database were created readable by other users under a
  common umask. New data folders are created with mode 0700, and the database,
  its backups, and `vault.json` with mode 0600. An existing folder keeps its
  mode; `doctor` says how to make it private.
- On Python older than 3.11, hooks failed with an import traceback. They now
  print one line that names the minimum, and `doctor` reports the `python3`
  that hooks would use, failing when it is too old, missing, or does not
  answer.
- `explain last` exited 1 when nothing was recorded yet. It now exits 0.
- `smoke-test` labeled the hook script it ran `installed_command`. The field is
  now `command_under_test`, because it is this package's copy, not the one a
  host installed.
- `scripts/validate_distribution.py` scanned the virtual environment that the
  Develop section creates and failed. It now reads only tracked files in a git
  work tree.

## 0.2.0 - 2026-09-29

### Update now if you use Claude Code

Version 0.1.0 shipped a default `hooks/hooks.json` file. Claude Code loads that
file as well as the hook file its manifest names, so the Codex hook commands
also ran under Claude Code and failed on every event. Version 0.2.0 removes the
file, and each host manifest now names its own hook file. Claude Code keeps its
cached plugin folder until the version changes, so update the plugin to pick up
the fix. `agent-efficiency doctor` reports Claude Code as not ready while a
stale `hooks/hooks.json` remains.

### Added: vault context

A vault is a git repository of short markdown notes about your own work.
Agent Efficiency can now add a bounded block of those notes to each session.

- `vault init` creates a tree with one classification: core, work, or
  private.
- `vault index` rebuilds the tree's `index.json` and `INDEX.md`.
- `vault check` and the installed pre-commit and pre-push hooks check
  classification, size caps, note schema, duplicate ids, the index, known
  credential shapes, and the push remote.
- `vault register` adds a tree to the local list of trees sessions read.
- `vault show` prints exactly what a session in a directory would receive.
- `vault migrate propose` and `vault migrate apply` move an existing folder of
  memory notes into vault trees in two steps, with a proposal you edit first.
  If any step of the apply fails, every tree it touched is restored, index
  files included.
- The project note for the working directory is chosen by `paths`, `repos`,
  and `branches`, from git metadata files, without running git.
- The whole block fits a 9,000 character allowance, and anything left out is
  counted in a closing line.
- `$agent-efficiency vault` reloads vault context in the current session.
- Reports count vault context selected, emitted, deferred, and unavailable.
- The database records one receipt per delivery decision with counts, two
  digests, and fixed codes. It never records note text, note ids, titles,
  hooks, vault paths, remotes, or branches.

### Vault delivery on each host

- Claude Code and Codex receive vault context at session start. After each
  compaction they receive it once, at whichever event comes first.
- Cursor receives vault context at session start. Cursor cloud agents send no
  session start, so it arrives with the first successful tool result.
- Cursor sends no event after a compaction that can carry context, so the next
  successful tool result after each `preCompact` restores vault context once.
- A Cursor prompt cannot carry context, so `$agent-efficiency vault` on Cursor
  reloads at the next successful tool result, once.

## 0.1.0 - 2026-08-26

First public release.

- Local verification and efficiency governance for Claude Code, Cursor, and
  Codex, with a shared runtime that uses only the Python standard library.
- Detection of repeated work, repeated failures, broad scans, and unverified
  code changes, with sparse guidance in `advise` mode.
- Project-defined checks with receipts bound to the exact Git and
  verification state.
- Reports of observed activity and matched `observe` and `advise` experiments.
- No storage of prompts, source code, raw commands, outputs, or transcripts.
