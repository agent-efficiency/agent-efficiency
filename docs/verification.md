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

## Preview prerequisite readiness

A check can declare prerequisites for a read-only planning report:

```toml
[[checks]]
id = "sanity"
command = ["python", "scripts/sanity.py"]

[[checks]]
id = "full"
command = ["python", "scripts/full_tests.py"]
depends_on = ["sanity"]
```

Run `agent-efficiency check full --plan` (or `check --changed --plan`). It
prints JSON containing selected `targets`, dependency-first `order`, prerequisite
receipt states, workspace availability, and `ready`. Exit 0 means the workspace
is readable and all direct/transitive prerequisites have current passing receipts;
exit 1 means not ready, and exit 2 means invalid configuration. A ready plan does
**not** mean the target check passed, that the release is ready, or that remote CI
succeeded. No verification command executes and no verification receipt is created.

Unknown, duplicate, self-referencing and cyclic prerequisites are rejected. A
latest failed receipt is not replaced by an older passing receipt. Missing, stale,
expired, blocked or inconclusive evidence does not establish readiness. Empty or
omitted `depends_on` preserves existing check/configuration digests.

This first implementation is **advisory only**: ordinary `check` execution does
not automatically run or enforce prerequisites, and hooks do not inject this
report. Run the prerequisite checks explicitly and preview again. It uses existing
workspace/configuration and optional environment freshness. Managed background
jobs and structured summaries are described below.

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

## Optional environment identity

`env_fingerprint = ["HOME"]` binds a check to the current value of an explicitly
allowed variable (`HOME` must also be in `env_allowlist`; `PATH` is implicitly
available). Values are held only in memory and combined with the check settings
using a private keyed digest. A changed value makes earlier evidence stale;
unset and empty are distinct. An invalid private key or oversized value makes
verification inconclusive, rather than using a public fallback hash of a secret.

This identifies declared environment selectors, not all external dependencies.
A directory path is not a digest of its installed packages. Use explicitly
configured preflight checks for dependency versions/capabilities; no dependency
installation or discovery command is run inside lifecycle hooks. Checks without
`env_fingerprint` keep their previous identities. Environment-bound checks require
the same declared values when running and inspecting evidence in any host.

Schema 9 preserves existing receipts and adds closed environment and structured
summary reason codes. The runtime retains a private
schema-8 backup before migrating. Do not point older runtimes at the upgraded
folder; restore a full backup to roll back. Development migrations are tested on
synthetic copies, never the active host's database.

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
| `env_fingerprint` | no | Up to 32 unique allowed environment names to bind privately to receipts. Optional; default `[]`. |
| `depends_on` | no | Unique configured check IDs whose current receipts `check --plan` requires. Advisory only; default `[]`. |
| `result_file` | no | Relative path to a newly written version-1 structured summary. Put it in an ignored output folder. |

The top level has `version = 1` and `mode`, which is `off`, `observe`,
`advise`, or `guard`. See [Guard mode](modes.md#guard-mode).

A check runs with only `PATH`, `PYTHONDONTWRITEBYTECODE=1`, and the variables
in `env_allowlist`. Add what your tests need, such as `HOME`, to that list.

## Managed background checks

```bash
agent-efficiency check unit --background
agent-efficiency jobs
agent-efficiency jobs JOB_ID
```

Launch returns a job, **not a passing receipt**. Repeated launches with the same
project/check/workspace/environment identity return the active job instead of
starting another process. The worker revalidates the launch identity before
executing. Completion records the receipt and terminal job state in one transaction;
duplicate completions cannot add receipts. A known cancellation cannot later turn
into a pass. Deadlines observed without completion are `unknown`, never success,
and are not automatically relaunched. Inspection is point-in-time, not a polling
instruction. State survives restarting the CLI.

Output is discarded unless `--output /new/private/check.log` is supplied. This
explicit user-owned log is outside telemetry and must not already exist. Choose a
path outside the project or an ignored output directory, otherwise creating it
changes the workspace. Logs can contain sensitive command output; protect them as
normal project artifacts. The telemetry database stores neither log paths nor output.

Managed workers stop their command process group on timeout/interruption on POSIX.
`JobLedger.cancel` records a **trusted** cancellation; it does not send a signal.
There is intentionally no command that kills an unverified stored PID. Lost workers
remain unknown until investigated. Non-POSIX descendant cleanup is not guaranteed.
External host background tools and automatic host completion notifications are not
intercepted by this CLI. Prefer the host's tracked background tool around a normal
synchronous `check` when you need that host's notifications and cancellation.

## Structured summaries

An opt-in `result_file` requires a newly written, regular JSON file no larger than
64 KiB. Its closed schema is:

```json
{"version":1,"planned":10,"passed":10,"failed":0,"baseline_failed":0,"setup_errors":0,"survived":0,"timed_out":0,"not_run":0}
```

All counts are nonnegative integers, `planned` is positive, and the outcome counts
must sum to `planned`. Unknown/duplicate fields and old unchanged files are rejected.
Only these counts enter receipt metadata; no test names, assertions, output or
arbitrary result labels are retained. `evidence show` includes the stored summary.

- Assertions and surviving mutations mean `fail`, even with exit 0.
- Failed baselines, setup errors and cases not run mean `inconclusive`.
- Timed-out cases mean `blocked`.
- Missing, stale or malformed summaries mean `inconclusive` (`structured_invalid`).
- An all-pass summary cannot override a nonzero command exit or changed workspace.

The fixed reason codes are `structured_failed`, `structured_survived`,
`structured_baseline_failed`, `structured_setup_error`, `structured_incomplete`,
`structured_timeout` and `structured_invalid`. This is an explicit machine contract,
not a parser that guesses success from arbitrary shell/test output. Existing checks
without `result_file` keep their original exit-status behavior and identity.

## Explain and explicitly reuse evidence

```bash
agent-efficiency evidence explain --json
agent-efficiency check unit --reuse-current
```

Explanations distinguish missing evidence, a failed latest run, changed workspace,
changed check/environment identity and expired age. They never upgrade a receipt.
Explicit reuse skips execution only when the latest receipt and all prerequisites
are current. It returns the same receipt ID; stale or failed evidence is rerun.
Default `check` behavior is unchanged. Reuse cannot be combined with `--background`
or `--plan`. Retained artifact comparisons should be configured as real checks:
identical declared source is not permission to substitute unreviewed output files.

## Ordered milestone continuity

Add ordered milestone tables to the project configuration:

```toml
[[milestones]]
id = "validation"
checks = ["unit"]

[[milestones]]
id = "landing-gates"
checks = ["full"]
```

`agent-efficiency workflow status` reconstructs readiness from the committed
configuration and current receipts. Each milestone includes transitive check
prerequisites. Later milestones are blocked until all earlier ones are ready;
changed files invalidate readiness across handoffs. Unknown checks and duplicate
IDs are rejected. Without a manifest, plan coverage is `unavailable`. `ready` is
not a review approval, release approval, remote CI claim or permission to publish.

## Coverage and rework annotations

```bash
agent-efficiency workflow record --kind retry --cause missing_prerequisite --duration-ms 12000
agent-efficiency workflow record --kind correction --cause plan_order
agent-efficiency workflow report
```

Only fixed causes are accepted: `missing_prerequisite`, `environment_mismatch`,
`probe_invalid`, `product_failure`, `tooling_failure`, `scope_changed`, `plan_order`
and `unknown`. No free-form correction/prompt/command text is stored. Ratings use
`--rating useful|neutral|distracting|unrated`; phases use
`--phase setup|runtime|review|tooling|unknown`. Optional `--session` must name an
existing session belonging to this project. Linked annotations appear separately
in the existing experiment's cohort report, including enrolled work that was not
accepted. They do not create acceptance evidence, intervention feedback or a new
cohort assignment.

Annotation costs may already be included in the host's all-in outcome costs;
that overlap is unknown, so annotations are **never added** to comparison totals.
Record complete setup/tooling/failed-work costs in the existing experiment outcome
contract; use the phase breakdown as supplemental explanation, not another bill.
Optional `--cost-usd` requires `--cost-basis estimate` or `reported`; estimates and reported
amounts stay separate. These are user annotations, not independently verified bills.
Duration sums are recorded attempt durations, **not** parallel wall time.

Reports include failed attempts rather than dropping them. Managed CLI result
coverage is supported; external background results are `null` (unavailable), not
zero failures. Environment and structured-result coverage are opt-in. Automatic
advice remains disabled here. A retry annotation is not proof an intervention
prevented a failure. Use the existing matched-task experiment workflow for accepted
results and all-in cost comparisons; these reports make no savings claim.

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
