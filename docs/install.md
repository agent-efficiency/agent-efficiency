# Install

This guide helps you install Agent Efficiency in Claude Code, Codex, or
Cursor, install the terminal command, keep both up to date, add the Claude Code
status line, and remove everything again.

## Before you start

- **Python 3.11 or newer as `python3` on your PATH.** The hooks run
  `python3 -S` from the PATH the host gives them. On an older Python, each hook
  prints one line that names the minimum and does nothing else. CI tests
  Python 3.11, 3.12, and 3.13.
- **git.** Verification checks and the vault use it.
- **[pipx](https://pipx.pypa.io/)** to install the terminal command. A virtual
  environment with pip also works.

Check your Python:

```bash
python3 --version
```

## The installer for Claude Code

`install.sh` at the root of the repository does every Claude Code step below
in one run: the plugin, the terminal command, optional vault trees, and a
final `doctor` check. It needs Claude Code, Python 3.11 or newer, and pipx or
uv.

```bash
git clone --branch v0.2.2 https://github.com/agent-efficiency/agent-efficiency.git
cd agent-efficiency
bash install.sh
```

It runs these steps, each under its own heading:

1. **Checks** that `claude` and `python3` 3.11 or newer are on PATH, and that
   each `--vault` folder is a vault tree. It stops with a plain message if
   not, before it changes anything.
2. **Claude Code settings.** It copies `settings.json` (in
   `$CLAUDE_CONFIG_DIR`, or `~/.claude`) to a backup file next to it, and
   creates the file if it is missing. It removes an `agent-efficiency`
   marketplace entry that points at a folder that no longer exists.
3. **The plugin.** It removes the `agent-efficiency` marketplace if it is
   there, adds it again, and installs the plugin for your user. The data
   that older releases kept in Claude Code's plugin data folder is kept.
4. **The command**, with `pipx install --force`, or with
   `uvx --from pipx pipx` when pipx is missing.
5. **Vault trees**, if you name any: it updates each tree's git hooks and
   registers it.
6. **`agent-efficiency doctor`**, then a reminder to run `/reload-plugins` in
   open sessions.

Options:

| Option | What it does |
| --- | --- |
| `--data-dir DIR` | Keeps the data in DIR. Creates DIR with mode 0700 and sets `env.AGENT_EFFICIENCY_DATA` in the Claude Code settings. |
| `--vault PATH` | Registers a vault tree and updates its git hooks. Repeat it for each tree. Create trees first; see [Vault](vault.md). |
| `--ref TAG` | Installs both the plugin and command from the same release tag. The default is `v0.2.2`. |
| `--no-command` | Skips the command install. An `agent-efficiency` command must already be on PATH. |

For example, with your own data folder and two vault trees:

```bash
bash install.sh --data-dir ~/ae-data --vault ~/notes/vault-core --vault ~/notes/vault-work
```

The installer is safe to run again. Each run makes a new settings backup and
brings the plugin, the command, and the vault hooks up to date. It does not
set up Codex or Cursor; follow the steps below for those.

## Claude Code by hand

```bash
claude plugin marketplace add agent-efficiency/agent-efficiency
claude plugin install agent-efficiency@agent-efficiency
```

Start a new session, or run `/reload-plugins` in an open one. In a session,
`/hooks` lists the hooks that are loaded.

Confirm the installed version:

```bash
claude plugin list
```

```text
Installed plugins:

  ❯ agent-efficiency@agent-efficiency
    Version: 0.2.2
    Scope: user
    Status: ✔ enabled
```

To install a fixed release instead of the latest one, add the marketplace
pinned to a tag:

```bash
claude plugin marketplace add "agent-efficiency/agent-efficiency#v0.2.2"
claude plugin install agent-efficiency@agent-efficiency
```

## The terminal command

The plugin runs inside the agent host. You also want the `agent-efficiency`
command in your own terminal: `doctor`, `check`, `report`, the `vault`
commands, and the vault git hooks all run there.

```bash
pipx install git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2
agent-efficiency --version
agent-efficiency doctor
```

`--version` prints `0.2.2`. `doctor` checks Python, the data folder, and the
copy of the plugin each host installed. Its first line is
`Agent Efficiency doctor: OK` when everything is ready.

With pip instead of pipx, use a virtual environment and put its `bin` folder
on your PATH:

```bash
python3 -m venv ~/.venvs/agent-efficiency
~/.venvs/agent-efficiency/bin/pip install git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2
export PATH="$HOME/.venvs/agent-efficiency/bin:$PATH"
agent-efficiency --version
```

The command and the plugin share one data folder,
`~/.local/share/agent-efficiency` (or `$XDG_DATA_HOME/agent-efficiency`). So a
mode you set or a vault you register in the terminal reaches your sessions.
Set `AGENT_EFFICIENCY_DATA` or pass `--data-dir` to use another folder; set it
the same way for the host, or the two will not share data.

Inside a Claude Code session, the agent can run the plugin's own copy of the
command, because Claude Code puts the plugin's `bin` folder on the PATH of the
commands the agent runs. Your terminal does not get that copy, so install the
command as shown above.

## Codex

```bash
codex plugin marketplace add agent-efficiency/agent-efficiency
codex plugin add agent-efficiency@agent-efficiency
```

Then start a new Codex session, type `/hooks`, and approve the Agent
Efficiency hooks. Codex does not run a plugin's hooks until you approve them.

Confirm the installed version:

```bash
codex plugin list
```

```text
PLUGIN                             STATUS              VERSION                     SOURCE
agent-efficiency@agent-efficiency  installed, enabled  0.2.2+codex.<build time>   ...
```

The `+codex.<build time>` part is a build stamp that tells Codex a new release
is a new version.

To pin a release, add the marketplace with `--ref`:

```bash
codex plugin marketplace add agent-efficiency/agent-efficiency --ref v0.2.2
codex plugin add agent-efficiency@agent-efficiency
```

## Cursor

Agent Efficiency does not publish a Cursor marketplace entry. What works today
is a copy of the plugin inside Cursor's local plugin folder. A link to a
folder outside that location does not load.

```bash
git clone --branch v0.2.2 https://github.com/agent-efficiency/agent-efficiency.git ~/src/agent-efficiency
mkdir -p ~/.cursor/plugins/local
cp -R ~/src/agent-efficiency/plugins/agent-efficiency ~/.cursor/plugins/local/agent-efficiency
```

Restart Cursor or run `Developer: Reload Window`. Open Customize and confirm
that Agent Efficiency is installed. `agent-efficiency doctor` checks this
folder:

```text
Cursor: ready, 0.2.2 (user scope) at /home/you/.cursor/plugins/local/agent-efficiency
```

For an existing install, including an outside link, keep the old entry as a
backup and copy the new plugin into place. Use a backup name that does not
already exist:

```bash
git -C ~/src/agent-efficiency fetch --tags
git -C ~/src/agent-efficiency checkout v0.2.2
mv ~/.cursor/plugins/local/agent-efficiency ~/.cursor/plugins/local/agent-efficiency-backup
cp -R ~/src/agent-efficiency/plugins/agent-efficiency ~/.cursor/plugins/local/agent-efficiency
```

Reload the window and run `agent-efficiency doctor` before removing the backup.

Cursor cloud agents do not report a session start. The first event they send
starts a local session record.

## Upgrade

Read the [changelog](../CHANGELOG.md) first. Some releases ask you to do
something after the upgrade.

### Claude Code

If you used the installer, check out the new release tag in your clone (here
`~/agent-efficiency`) and run it again:

```bash
git -C ~/agent-efficiency fetch --tags
git -C ~/agent-efficiency checkout v0.2.2
bash ~/agent-efficiency/install.sh
```

If you added the marketplace by hand without a tag:

```bash
claude plugin marketplace update agent-efficiency
claude plugin update agent-efficiency@agent-efficiency
claude plugin list
```

Restart your sessions to load the new version.

If you added the marketplace pinned to a tag, such as
`agent-efficiency/agent-efficiency#v0.2.1`, the update commands keep you on
that tag and report that the plugin is already at the latest version. Remove
the marketplace and add it again with the new tag. Removing the marketplace
also removes the plugin.

```bash
claude plugin marketplace remove agent-efficiency
claude plugin marketplace add "agent-efficiency/agent-efficiency#v0.2.2"
claude plugin install agent-efficiency@agent-efficiency
claude plugin list
```

`claude plugin list` must show `Version: 0.2.2`.

Coming from 0.2.0 or earlier? Those releases kept session history in Claude
Code's plugin data folder, which removing the marketplace deletes. Install the
new terminal command and run `agent-efficiency doctor` first. It copies that
history into the shared data folder, and its `unused_data_dirs` line then says
`copied`.

### Codex

If you added the marketplace without a ref:

```bash
codex plugin marketplace upgrade agent-efficiency
codex plugin add agent-efficiency@agent-efficiency
codex plugin list
```

If you pinned a ref, remove the marketplace and add it again with the new ref:

```bash
codex plugin marketplace remove agent-efficiency
codex plugin marketplace add agent-efficiency/agent-efficiency --ref v0.2.2
codex plugin add agent-efficiency@agent-efficiency
codex plugin list
```

### The terminal command

```bash
pipx install --force git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2
agent-efficiency --version
```

## Status line for Claude Code

Claude Code can show a status line under the prompt. Agent Efficiency can fill
it and, at the same time, record the cost and token totals that Claude Code
passes to it. Without the status line, reports show those totals as
unavailable.

Add this to `~/.claude/settings.json`, next to the settings already there:

```json
{
  "statusLine": {
    "type": "command",
    "command": "agent-efficiency statusline"
  }
}
```

The status line then reads like this:

```text
AE advise | $0.412 | 37% ctx | 8 tools/2 fail | 1 checks
```

That is the session mode, the session cost, how full the context window is,
tool calls and failed tool calls, and validation commands run (tests, linters,
type checkers, and builds).

For each session it records the cost, input and output tokens, cache read and
cache creation tokens, the percent of the context window used, and lines added
and removed. It records nothing while the session is `off`. It needs the
`agent-efficiency` command on the PATH that Claude Code uses.

## Uninstall

Remove each part you installed.

**Claude Code:**

```bash
claude plugin uninstall agent-efficiency@agent-efficiency --keep-data
claude plugin marketplace remove agent-efficiency
```

`--keep-data` keeps Claude Code's plugin data folder,
`~/.claude/plugins/data/agent-efficiency-agent-efficiency`. Since 0.2.1,
Agent Efficiency keeps nothing there. Only a store from 0.2.0 or earlier that
was never copied lives there. Without `--keep-data`, Claude Code deletes that
folder. Both commands leave the cached plugin files under
`~/.claude/plugins/cache/agent-efficiency`, which you can delete. Remove the
`statusLine` entry from `~/.claude/settings.json` if you added it.

**Codex:**

```bash
codex plugin remove agent-efficiency@agent-efficiency
codex plugin marketplace remove agent-efficiency
```

This leaves an empty folder, `~/.codex/plugins/cache/agent-efficiency`, which
you can delete.

**Cursor:** delete the link and the clone.

```bash
rm ~/.cursor/plugins/local/agent-efficiency
rm -rf ~/src/agent-efficiency
```

**The terminal command:**

```bash
pipx uninstall agent-efficiency
```

**Vault git hooks.** In each vault tree, stop git from using the vault hooks.
Otherwise every commit in that tree fails once the command is gone.

```bash
git -C ~/notes/vault-core config --local --unset core.hooksPath
git -C ~/notes/vault-work config --local --unset core.hooksPath
```

Your notes stay where they are.

**The data folder.** This deletes your session history, receipts, and the list
of vault trees. It cannot be undone.

```bash
rm -rf ~/.local/share/agent-efficiency
```

If you set `AGENT_EFFICIENCY_DATA` or `XDG_DATA_HOME`, delete that folder
instead.
