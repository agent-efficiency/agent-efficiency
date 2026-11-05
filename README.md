# Agent Efficiency

[![CI](https://github.com/agent-efficiency/agent-efficiency/actions/workflows/ci.yml/badge.svg)](https://github.com/agent-efficiency/agent-efficiency/actions/workflows/ci.yml)
[![Latest release](https://img.shields.io/github/v/release/agent-efficiency/agent-efficiency)](https://github.com/agent-efficiency/agent-efficiency/releases/latest)
[![License: MIT](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Python 3.11 to 3.13](https://img.shields.io/badge/python-3.11%20%7C%203.12%20%7C%203.13-blue)](pyproject.toml)

Agent Efficiency is a plugin for the coding agents Claude Code, Codex, and
Cursor. It watches each agent session on your own machine. It points out
repeated work and repeated failures, and it checks that a code change was
tested before the agent says it is done. It is for developers who use a coding agent every day
and want fewer wasted turns and proof that a change was checked. It makes no
model calls and no network requests.

## What it does

- **Spots wasted work.** It notices repeated commands, repeated failures, broad
  file scans, and code edits with no test after them. Then it adds one short
  note to the session. A mode sets how much it does: `off`, `observe`,
  `advise` (the default), or `guard`.
- **Records test results.** You list your project's test commands in
  `agent-efficiency.toml`. `agent-efficiency check` runs one and saves a
  receipt: the result, tied to the exact commit and file contents it ran on.
  When the files change, the receipt is marked stale.
- **Loads your own notes.** A vault is a git repository of short notes about
  your work. Each new session gets the notes that match its folder, in at most
  9,000 characters.
- **Stays on your machine.** It keeps counts and states in one local SQLite
  file. It never stores prompts, code, commands, tool output, or transcripts.
- **Works in three hosts.** Claude Code, Codex, and Cursor share one runtime,
  written with only the Python standard library.

## Quick start for Claude Code

The tagged install examples target version 0.2.2 and require that release to
be published. To review an unreleased checkout, use the
[local development steps](docs/development.md#try-your-checkout-in-a-host).

You need Claude Code, Python 3.11 or newer as `python3` on your PATH, and
[pipx](https://pipx.pypa.io/) or uv for the terminal command.

1. Clone the release and run the installer. It installs the plugin and the
   `agent-efficiency` command, then checks the setup:

   ```bash
   git clone --branch v0.2.2 https://github.com/agent-efficiency/agent-efficiency.git
   cd agent-efficiency
   bash install.sh
   ```

   A ready setup ends like this:

   ```text
   Agent Efficiency doctor: OK
   ...
   Claude Code: ready, 0.2.2 (user scope) at /home/you/.claude/plugins/cache/agent-efficiency/agent-efficiency/0.2.2
   ...
   Run /reload-plugins in any open Claude Code session, or start a new session.
   ```

   If doctor prints `FAILED`, the lines below it say what is wrong and how to
   fix it. See [Troubleshooting](docs/troubleshooting.md).

   The same steps by hand, without the clone:

   ```bash
   claude plugin marketplace add agent-efficiency/agent-efficiency
   claude plugin install agent-efficiency@agent-efficiency
   pipx install git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2
   agent-efficiency doctor
   ```

2. Start a new Claude Code session. To see what it recorded, type this as an
   ordinary chat message (it is not a slash command):

   ```text
   $agent-efficiency status
   ```

   The plugin answers with one line, such as
   `Agent Efficiency: advise | turns 0 | tools 0 | failures 0 | nudges 0 | cost host metric unavailable.`

**Codex** needs two `codex plugin` commands and one approval in `/hooks`.
**Cursor** works from a local plugin folder only, for now. See
[Install](docs/install.md#codex) for both.

## Documentation

| Guide | Read this to |
| --- | --- |
| [Install](docs/install.md) | install, upgrade, add the status line, or remove everything |
| [Modes](docs/modes.md) | choose how much the plugin does, per session or by default |
| [Verification](docs/verification.md) | set up checks and read pass, fail, stale, and the other results |
| [Vault](docs/vault.md) | give every session your own project notes |
| [Reports](docs/reports.md) | read what was recorded and run a fair experiment |
| [Privacy](docs/privacy.md) | see exactly what is stored, and where |
| [Troubleshooting](docs/troubleshooting.md) | fix a setup that does not work |
| [Architecture](docs/architecture.md) | see how the parts fit, with diagrams |
| [Measurement](docs/measurement.md) | measure whether the plugin is worth its cost |
| [Development](docs/development.md) | build and test the project, and see its limits |

## Privacy

All data stays in one local folder, `~/.local/share/agent-efficiency` by
default, created so that only you can read it. It holds counts, states, times,
and one-way fingerprints, never your prompts, code, or output. See
[Privacy](docs/privacy.md).

## Contributing, security, and license

- To contribute, read [CONTRIBUTING.md](CONTRIBUTING.md) and the
  [Code of Conduct](CODE_OF_CONDUCT.md).
- To report a vulnerability, follow [SECURITY.md](SECURITY.md).
- Agent Efficiency is released under the [MIT License](LICENSE).
