# Privacy

This guide helps you see exactly what Agent Efficiency stores, where it stores
it, and who can read it.

## In short

- Everything stays on your machine. The hooks make no network requests and no
  model calls.
- It stores counts, states, times, and one-way fingerprints (digests that
  cannot be turned back into the text).
- It never stores your prompts, code, commands, tool output, agent messages,
  or transcripts.

## Where the data lives

One data folder serves the terminal command and every host:

1. the folder named by `--data-dir`, if you pass it;
2. else `$AGENT_EFFICIENCY_DATA`, if set;
3. else `$XDG_DATA_HOME/agent-efficiency`, if `XDG_DATA_HOME` is set;
4. else `~/.local/share/agent-efficiency`.

`agent-efficiency doctor` prints the folder in its `data_dir` line. It holds:

| File | What it is |
| --- | --- |
| `agent-efficiency.db` | The SQLite database, with its `-wal` and `-shm` files. |
| `agent-efficiency.db.schema-N.bak` | A backup made before a database upgrade. |
| `vault.json` | The list of registered vault trees, by path. |
| `knowledge/` | The guidance pack that sessions pin. |
| `.plugin-data-copy.lock`, `plugin-data-copy.json` | A lock file, and a record of a store copied in from an earlier release. |

## Who can read it

Agent Efficiency creates the data folder with mode 0700 and its files with mode
0600, so only you can read them. A folder that already existed keeps its mode.
`doctor` checks the folder and every file in it, and names anything other users
can access:

```text
data_dir_permissions: only you can read the data folder
```

If it names the folder or a file, run the command it prints:

```bash
chmod -R go-rwx ~/.local/share/agent-efficiency
```

## What the database holds

- project folder names and one-way project keys;
- host and model names;
- event times and fixed event classes;
- one-way fingerprints of actions;
- counts and durations;
- verification receipts: check ids, results, reason codes, commit ids, and
  digests;
- experiment labels and outcomes;
- cost and token totals, when the Claude Code status line provides them;
- vault delivery receipts: counts, two digests, and fixed codes.

## What it never holds

- prompt text;
- source code or patches;
- commands as typed;
- tool output or check output;
- agent messages;
- transcripts or their paths;
- environment values;
- experiment task descriptions or evidence content;
- vault note text, note ids, titles, hooks, vault paths, remotes, or branches.

Vault note text goes from your vault to the session that receives it and
nowhere else.

## Still sensitive

Folder names, host and model names, times, and counts can still say something
about your work. Keep the data folder private, and do not commit it or attach
it to an issue.

## Remove it

See [Uninstall](install.md#uninstall). The full security model is in
[SECURITY.md](../SECURITY.md).
