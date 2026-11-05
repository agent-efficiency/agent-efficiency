# Development

This guide helps you build and test Agent Efficiency from source, and explains
what it cannot do. To send a change, read [CONTRIBUTING.md](../CONTRIBUTING.md)
first.

## Set up

You need Python 3.11 or newer and git.

```bash
git clone https://github.com/agent-efficiency/agent-efficiency.git
cd agent-efficiency
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

## Run every check

These are the same checks CI runs on Python 3.11, 3.12, and 3.13:

```bash
python -m unittest discover -s tests -v
python scripts/build_policy_pack.py --check
python scripts/build_guidance_pack.py --check
python scripts/validate_distribution.py
python scripts/check_wheel_install.py
python -m agent_efficiency doctor --json
```

When the Claude Code CLI is installed, also validate the plugin:

```bash
claude plugin validate ./plugins/agent-efficiency --strict
```

`validate_distribution.py` reads only the files git tracks, so a `.venv` in
the checkout does not affect it. It also checks versions, manifests, local
Markdown links, and the size of the runtime package.

## Try your checkout in a host

Install the plugin from your checkout instead of from GitHub:

```bash
claude plugin marketplace add /absolute/path/to/agent-efficiency
claude plugin install agent-efficiency@agent-efficiency
```

For Codex, use the same path with `codex plugin marketplace add`. For Cursor,
copy the plugin folder as shown in [Install for Cursor](install.md#cursor).

To run fixture events through this checkout's hook command, without a live
session:

```bash
agent-efficiency smoke-test claude
agent-efficiency smoke-test codex
agent-efficiency smoke-test cursor
```

A smoke test does not replace a live session. The tests also include
fixtures for every declared Claude Code, Cursor, and Codex event under
`tests/fixtures/hosts/`.

## Layout

- `plugins/agent-efficiency/` is the installed plugin: one manifest and one
  hook file per host, the skill, and the Python package in `src/`.
- `scripts/` holds the pack builders and release checks.
- `tests/` holds the unit tests and host fixtures.

See [Architecture](architecture.md) for how the parts fit.

## Limits

- Hook events are useful observations. They are not a complete billing record
  or a security boundary.
- The signals are fixed rules, and fixed rules can be wrong.
- Each host exposes different events and effects, so behavior differs where a
  host cannot do something.
- Model choice is advice only. Agent Efficiency never switches models.
- A claim of savings needs a comparable baseline and evidence that the task was
  accepted. See [Measurement](measurement.md).
- In `off` mode the hooks still start briefly, so an in-session `on` can be
  recognized.
- An observed `git fetch` before a new branch proves only that the session ran
  a fetch. It does not prove that the remote was current.
