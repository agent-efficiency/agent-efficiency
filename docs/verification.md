# Verification

This guide helps you define the checks that prove a change works, run them,
and read the result. A check is a command you already use, such as your unit
tests. Agent Efficiency runs it and saves a receipt: a record of the result
tied to the exact commit and file contents it ran on.

## Set up a check

Run this at the root of a git repository with at least one commit:

```bash
cd ~/code/app
agent-efficiency init
```

```text
Created /home/you/code/app/agent-efficiency.toml
```

`init` writes this file. Edit the command to match your project:

```toml
version = 1
mode = "advise"

[[checks]]
id = "unit"
command = ["python", "-m", "unittest", "discover", "-s", "tests"]
applies_to = ["**/*.py"]
required_for = ["code"]
timeout_seconds = 300
```

If your system has no `python` command, write `"python3"` instead. Commit the
file with your project.

## Run a check

```bash
agent-efficiency check unit
```

The command's own output streams to your terminal and is not stored. The last
line is the result:

```text
Agent Efficiency check unit: pass (receipt 1)
```

To run every check that applies to the files you changed:

```bash
agent-efficiency check --changed
```

When no check applies to the changed files, it prints
`No configured checks apply to the current changes.`

`check` exits with 0 when every check passed, 1 when any result is not a pass,
and 2 when the configuration cannot be read or names no such check.

## Read the results

```bash
agent-efficiency evidence list
```

```text
unit: current
```

`evidence list` shows the state of the latest receipt of each check against
the files as they are now. Edit a file and it changes:

```text
unit: stale
```

A result that is not a pass also says why:

```text
unit: fail: The check command exited with status 1.
```

To see one receipt in full, or everything as JSON:

```bash
agent-efficiency evidence show 1
agent-efficiency evidence list --json
```

## Results and states

A check run ends in one of four results:

| Result | Meaning |
| --- | --- |
| `pass` | The command exited with 0, and the files were the same before and after it ran. |
| `fail` | The command exited with a status other than 0. |
| `blocked` | The command could not run to the end: it timed out, or it could not be started. |
| `inconclusive` | The result cannot be tied to the files: the workspace could not be read, or the check changed files. |

`evidence list` then compares the latest receipt of each check with the
workspace now. That gives one of these states:

| State | Meaning |
| --- | --- |
| `current` | The latest receipt is a pass, and nothing it covers has changed since. |
| `stale` | The latest receipt is a pass, but a file, the check settings, or the commit changed after it, or it is older than `max_age_seconds`. |
| `missing` | The check has never run in this project. |
| `fail`, `blocked`, `inconclusive` | The latest receipt had this result. |

Only `current` counts as proof. Counts of test commands the agent ran never do.

## Reason codes

Every result that is not a pass records one reason code. `check`,
`evidence list`, and `evidence show` print it with one sentence:

| Code | Result | Meaning and fix |
| --- | --- | --- |
| `exit_nonzero` | `fail` | The command exited with a nonzero status. Fix what it reports. |
| `timeout` | `blocked` | The command did not finish within `timeout_seconds`. Raise the limit or make the check faster. |
| `not_started` | `blocked` | The command could not be started. Check that the program exists on PATH. |
| `workspace_unknown` | `inconclusive` | The workspace could not be read before the check. Run checks at the root of a git repository with at least one commit. |
| `workspace_changed` | `inconclusive` | The check changed files in the workspace. Commit or ignore the files it writes, then run it again. |
| `workspace_unreadable` | `inconclusive` | The workspace could not be read after the check. |

Example:

```text
Agent Efficiency check unit: inconclusive (receipt 3)
  reason: workspace_changed: The check changed files in the workspace, so its result does not describe the files as they were checked. Commit or ignore the files the check writes, then run it again.
```

Receipts from before 0.2.1 show that no reason was recorded.

## What a receipt is tied to

A receipt records a digest (a one-way fingerprint) of:

- the full git commit;
- the changes to tracked files;
- the content of untracked files that git does not ignore, except files
  inside the tool cache folders `__pycache__`, `.pytest_cache`, `.mypy_cache`,
  and `.ruff_cache`;
- the settings in `agent-efficiency.toml`.

It also records the check id, exit status, and start and finish times. It
never records the command output.

## The check settings

| Key | Required | Meaning |
| --- | --- | --- |
| `id` | yes | Lowercase letters, digits, dots, and hyphens, starting with a letter. |
| `command` | yes | The command as an array of arguments. It runs without a shell, so pipes and `&&` do not work. |
| `applies_to` | no | File patterns. `check --changed` and guard mode use it to pick checks. |
| `required_for` | no | `["code"]` makes the check required after code changes in guard mode. |
| `timeout_seconds` | no | 1 to 3600. The default is 300. |
| `max_age_seconds` | no | A pass older than this is stale. |
| `env_allowlist` | no | Names of environment variables the check may see. |

The top level has `version = 1` and `mode`, which is `off`, `observe`,
`advise`, or `guard`. See [Guard mode](modes.md#guard-mode).

A check runs with only `PATH`, `PYTHONDONTWRITEBYTECODE=1`, and the variables
in `env_allowlist`. Add what your tests need, such as `HOME`, to that list.

## Troubleshooting

- **`inconclusive` with `workspace_changed`.** The check wrote a file, such as
  a coverage report or a build folder. Add it to `.gitignore`, commit that,
  and run the check again.
- **`inconclusive` with `workspace_unknown`.** Run `check` at the root of the
  git repository, and make at least one commit first.
- **`blocked` with `not_started`.** The program in `command` is not on the
  PATH the check sees. Use the full path, or check the spelling.
- **`verification config not found`.** Run `agent-efficiency init`, or run the
  command from the folder that holds `agent-efficiency.toml`.
