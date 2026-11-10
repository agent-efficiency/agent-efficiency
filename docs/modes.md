# Modes

This guide helps you choose how much Agent Efficiency does in a session, and
change it in a session, from the terminal, or for every new session.

## The four modes

| Mode | What it records | What it adds to the session |
| --- | --- | --- |
| `off` | Nothing | Nothing |
| `observe` | Counts and states | Nothing |
| `advise` | Counts and states | Short notes when work repeats or keeps failing |
| `guard` | Counts and states | Everything `advise` adds, plus one required check before the agent stops |

`advise` is the default. Here is what each mode means in practice:

- **`off`**: the hooks still start for a moment on each event, so that a later
  `$agent-efficiency on` can be recognized. Nothing else happens, and vault
  context is not read.
- **`observe`**: use it to measure without changing anything. The plugin
  records what it would have said but says nothing. Vault context is selected
  and counted, but not added.
- **`advise`**: the plugin adds a note when it sees repeated commands,
  repeated failures, broad file scans, repeated installs, many subagents, a
  new branch with no fetch before it, or a compaction during a failure loop.
  It adds one short guidance card to a large build, research, or review task.
  When the agent tries to stop after a code edit with no test after it, it
  asks the agent to check the change. Each note appears at most once per turn,
  with at most 3 notes per turn and 8 per session.
- **`guard`**: when the agent tries to stop after changing code and a required
  check has no current passing result, the plugin sends the agent back with the
  exact command to run. It does this at most once per turn. Guard needs your
  consent in the project: see [Guard mode](#guard-mode).

## Change the mode in a session

Type one of these as an ordinary chat message in Claude Code, Codex, or
Cursor. It is not a slash command; send it the way you send any other message:

```text
$agent-efficiency on
$agent-efficiency observe
$agent-efficiency guard
$agent-efficiency off
$agent-efficiency status
$agent-efficiency vault
$agent-efficiency help
```

The hook answers these messages itself, before the model sees them, so they do
not cost a model turn. `on` is the same as `advise`. The change applies to the
current session only. `status` shows the mode and the session counts. `vault`
reloads your vault context; see [Vault](vault.md).

## Change the mode from the terminal

```bash
agent-efficiency mode observe
agent-efficiency mode advise
agent-efficiency mode off
```

`mode` changes the most recent session whose working folder is the folder you
run it in. Run it from the same folder the session started in. If no session
started there, it says so and changes nothing:

```text
No recorded session in this project. In an active agent chat, send `$agent-efficiency observe`; use `agent-efficiency default observe` for future sessions.
```

To change one session from anywhere, name it with `--session ID`. The short
forms `agent-efficiency on`, `off`, `observe`, `advise`, and `guard` do the
same as `mode`.

## Change the default for new sessions

```bash
agent-efficiency default observe
```

```text
Default mode for future sessions: observe
```

Sessions that are already running keep their mode. The default lives in the
shared data folder, so the terminal command and every host see it.

## Guard mode

Guard mode needs two things:

1. `mode = "guard"` in the project's `agent-efficiency.toml`.
2. At least one check with `required_for = ["code"]`.

See [Verification](verification.md) for the file format. Without the project
setting, `$agent-efficiency guard` answers:

```text
Guard mode requires mode = "guard" in agent-efficiency.toml for this repository.
```

When the agent tries to stop after changing code, guard looks at each required
check that applies to the changed files. If one is missing, failed, stale,
blocked, or inconclusive, it asks the agent once to run
`agent-efficiency check CHECK_ID` and review the result. The agent then
continues the same turn.

A project whose file says `mode = "guard"` turns `advise` into `guard` for
sessions in that project. `observe` and `off` stay as they are.

### When the config changes during a session

The first guard event of a session pins the digest of `agent-efficiency.toml`.
After that, guard stays on for the rest of the session in each of these cases:

| The file | What guard does |
| --- | --- |
| changed, including a change to `mode = "advise"` | When the agent stops after a code change, it is asked once per turn to tell you. Required checks wait until the change is accepted or undone. |
| exists but is not a valid config | The same. This also turns guard on in an `advise` session. |
| removed | The same. |

The agent cannot accept a changed file. To accept it, type
`$agent-efficiency guard` in the session, or `$agent-efficiency advise` if the
file no longer sets guard. After the file was removed, type
`$agent-efficiency advise` to leave guard. Restoring the file also ends the
state. The pin lasts for one session: a new session pins the file it finds.

`agent-efficiency mode guard` in a terminal pins the current file too. An agent
with shell access can run it. `agent-efficiency doctor` shows whether Claude
Code asks before it runs `agent-efficiency` in the project.
