# Troubleshooting

This guide helps you fix a setup that does not work. Start with
`agent-efficiency doctor`. Its first line is `OK` or `FAILED`, and each problem
it finds comes with the command that fixes it.

```bash
agent-efficiency doctor
agent-efficiency doctor --cwd ~/code/app
```

`--cwd` checks a project folder other than the one you are in.

## Symptoms

| Symptom | Cause | Fix |
| --- | --- | --- |
| `agent-efficiency: command not found` | The plugin does not put the command in your terminal. | Install it: `pipx install git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2`. If the command is still not found, run `pipx ensurepath` and open a new terminal. |
| Nothing happens in one project, but it works elsewhere. | A settings file in that project turns the plugin off. | Run `agent-efficiency doctor --cwd PROJECT` and the `fix:` command it prints. See [Plugin disabled in a folder](#plugin-disabled-in-a-folder). |
| `doctor` says Claude Code is not ready because of a stale `hooks/hooks.json`. | A copy of the 0.1.0 hook file is left in the installed plugin folder. Claude Code runs it as well as the right one, and it fails on every event. | Update the plugin, or delete the file `doctor` names. See [Stale hook file](#stale-hook-file). |
| `status` and `report` in the terminal show no sessions, or fewer than you had. | Before 0.2.1, sessions were stored in the host's plugin data folder, apart from the terminal command. | Update to 0.2.2 and run `agent-efficiency doctor`. See [History in an old data folder](#history-in-an-old-data-folder). |
| In Codex, nothing happens and `$agent-efficiency status` gets a normal model answer. | Codex has not been allowed to run the plugin's hooks. | In a Codex session, type `/hooks` and approve the Agent Efficiency hooks. Then start a new session. |
| `$agent-efficiency status` is sent to the model like any other text. | The hooks are not running in this session. | Start a new session after you install. In Claude Code, run `/hooks` to see if the hooks are loaded, and run `agent-efficiency doctor --cwd` for the project folder. |
| `doctor` fails on `hook_python`. | The `python3` on PATH is missing or older than 3.11. | Install Python 3.11 or newer and make it the `python3` on the PATH your agent host starts with. |
| `doctor` reports different or unknown database schemas for an installed plugin. | The command and plugin came from different builds, or the installed schema declaration cannot be read. | Install both from the same tested release. Follow the [schema 8 upgrade steps](schema-8-upgrade.md) before migrating data or restarting sessions. |
| `mode` says `No recorded session in this project`. | `mode` changes the latest session that started in the current folder, and none did. | Run it from the folder the session started in, pass `--session ID`, or send `$agent-efficiency MODE` in the session. |
| A vault commit or push fails with `agent-efficiency command is not on PATH`. | The vault git hooks need the terminal command. | Install the command, or turn the hooks off. See [Uninstall](install.md#uninstall). |
| A check result is `inconclusive`. | The workspace could not be read, or the check changed files. | Read the reason code. See [Verification troubleshooting](verification.md#troubleshooting). |
| A new vault note does not reach sessions. | The tree's `index.json` is older than the notes. | Run `agent-efficiency vault index TREE` and commit `index.json` and `INDEX.md`. |
| `doctor` says other users can access the data folder or files in it. | The folder existed before Agent Efficiency, or a file was made readable. | Run the `chmod -R go-rwx` command it prints. |

## Plugin disabled in a folder

Claude Code reads settings from several files, and the last one that names the
plugin decides. A project can turn the plugin off with its own
`.claude/settings.json`. `doctor --cwd` shows which file decides:

```text
Claude Code in /home/you/code/app: installed but disabled in this folder: /home/you/code/app/.claude/settings.json sets agent-efficiency@agent-efficiency to false in enabledPlugins
  fix: enable it again, in /home/you/code/app: claude plugin enable agent-efficiency@agent-efficiency --scope project
```

Run the `fix:` command, then start a new session in that folder.

## Stale hook file

Version 0.1.0 shipped a default `hooks/hooks.json`. Claude Code loads that file
as well as the hook file the plugin names, so the wrong hook commands ran on
every event. Later releases do not ship it, but a copy can stay in an installed
plugin folder. `doctor` reports it:

```text
Claude Code: not ready, 0.2.2 (user scope) at /home/you/.claude/plugins/cache/agent-efficiency/agent-efficiency/0.2.2
  fix: remove the stale default hook file /home/you/.claude/plugins/cache/agent-efficiency/agent-efficiency/0.2.2/hooks/hooks.json: the host runs it in addition to its own hook file, with an empty plugin root
```

Delete the file it names, or update the plugin (see
[Upgrade](install.md#upgrade)). Then restart your sessions.

## History in an old data folder

Versions 0.2.0 and earlier kept session history for Claude Code and Codex in
the host's plugin data folder, such as
`~/.claude/plugins/data/agent-efficiency-agent-efficiency`. The terminal
command used `~/.local/share/agent-efficiency`. So terminal commands never saw
those sessions.

From 0.2.1 on, everything uses one data folder. The first hook or terminal
command that finds no database there copies the old store in once. `doctor`
lists each old folder in `unused_data_dirs` and says whether it was copied. If
the shared folder already had its own database, nothing is copied, and
`doctor` prints the steps that copy it while keeping both histories:

```text
unused_data_dirs:
  /home/you/.claude/plugins/data/agent-efficiency-agent-efficiency: not copied, because /home/you/.local/share/agent-efficiency already had its own data; it lacks 1 of the 1 sessions here, and nothing reads this folder now
    to copy it: quit every agent session; move /home/you/.local/share/agent-efficiency/agent-efficiency.db, /home/you/.local/share/agent-efficiency/agent-efficiency.db-wal, /home/you/.local/share/agent-efficiency/agent-efficiency.db-shm into a folder of your choosing; run agent-efficiency doctor, which copies the store from /home/you/.claude/plugins/data/agent-efficiency-agent-efficiency; then keep the moved files if you want the sessions they hold, or delete them
```

Removing the Claude Code marketplace or uninstalling the plugin without
`--keep-data` deletes the old plugin data folder. Run `doctor` before you do.

## Still stuck

Open an issue with the output of `agent-efficiency doctor --json`. Remove
anything private first. See [SUPPORT.md](../SUPPORT.md).
