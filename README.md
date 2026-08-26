# Agent Efficiency

Agent Efficiency is a local verification and efficiency governor for coding
agents. It supports Claude Code, Cursor, and Codex.

It observes agent lifecycle events, records privacy-safe operational facts,
and gives sparse guidance when a session repeats work, expands scope without a
clear reason, or tries to finish after an unverified code change.

The goal is:

> Reduce the total cost of reaching a verified, accepted result without
> reducing scope, security, evidence, or quality.

Agent Efficiency makes no model calls. Its runtime uses only the Python
standard library.

## What it does

- Detects repeated actions, repeated failures, broad scans, repeated installs,
  excessive delegation, unsafe branch starts, and compaction during a failure
  loop.
- Gives one bounded guidance card for a substantial build, research, or review
  task.
- Records whether code edits were followed by validation.
- Runs project-defined checks and creates receipts bound to the exact Git and
  verification state.
- Marks a passing receipt stale after relevant files or verification settings
  change.
- Reports observed failures, repeated work, verification, interventions,
  runtime overhead, and host-provided cost data.
- Supports matched observe and advise experiments.
- Keeps prompts, source code, raw commands, outputs, assistant messages, and
  transcripts out of persistent storage.

## Supported hosts

| Host | Native package | Lifecycle guidance | Verification receipts |
| --- | --- | --- | --- |
| Claude Code | Yes | Yes | Yes |
| Cursor | Yes | Yes | Yes |
| Codex | Yes | Yes | Yes |

Each host uses its own hook manifest and a shared runtime. Host adapters declare
which lifecycle events and effects they support. Unsupported effects fall back
to a safe notification or no action.

## Modes

| Mode | Local telemetry | Guidance |
| --- | --- | --- |
| `off` | None | None |
| `observe` | Privacy-safe facts | None |
| `advise` | Privacy-safe facts | Sparse and deduplicated |
| `guard` | Privacy-safe facts and receipts | Advice plus one required-check continuation |

The default mode is `advise`. Guard mode also requires project consent in
`agent-efficiency.toml`.

In a supported agent session:

```text
$agent-efficiency on
$agent-efficiency observe
$agent-efficiency guard
$agent-efficiency off
$agent-efficiency status
```

`on` is an alias for `advise`. These controls are intercepted by the hook, so
changing mode does not require a model response.

The CLI can also control the most recently observed session:

```bash
agent-efficiency mode advise
agent-efficiency mode observe
agent-efficiency mode off
agent-efficiency default advise
```

## Install for Claude Code

```bash
claude plugin marketplace add agent-efficiency/agent-efficiency
claude plugin install agent-efficiency@agent-efficiency
```

Start a new session or run `/reload-plugins`. Use `/hooks` to inspect the
loaded hooks.

For local development:

```bash
claude plugin marketplace add /absolute/path/to/agent-efficiency
claude plugin install agent-efficiency@agent-efficiency
```

## Install for Cursor

The Cursor manifest is
`plugins/agent-efficiency/.cursor-plugin/plugin.json`.

For local development:

```bash
mkdir -p ~/.cursor/plugins/local
ln -s /absolute/path/to/agent-efficiency/plugins/agent-efficiency \
  ~/.cursor/plugins/local/agent-efficiency
```

Restart Cursor or run `Developer: Reload Window`. Open Customize and confirm
that Agent Efficiency is installed for the intended user or project scope.

Cursor cloud does not expose the same local session boundaries as the desktop
host. The first observed event creates a provisional local session.

## Install for Codex

The repository contains a Codex marketplace at
`.agents/plugins/marketplace.json`. Add the repository marketplace in Codex,
install Agent Efficiency, and start a new session. Use `/hooks` to inspect and
trust the bundled command hook.

The canonical Codex hook manifest is
`plugins/agent-efficiency/hooks/codex-hooks.json`.

## Configure verification

Create a project configuration:

```bash
agent-efficiency init
```

Example:

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

Commands are argument arrays. Agent Efficiency does not run them through a
shell.

Run one check or all checks that apply to changed files:

```bash
agent-efficiency check unit
agent-efficiency check --changed
```

The check output streams to the terminal and is not stored. The resulting
receipt binds:

- the full Git commit;
- tracked changes;
- untracked file content;
- the verification configuration;
- the check result and time.

Inspect receipts:

```bash
agent-efficiency evidence list
agent-efficiency evidence list --json
agent-efficiency evidence show RECEIPT_ID
```

Non-Git workspaces and unreadable workspace state are inconclusive. Activity
counters never count as verification receipts.

## Diagnose the installation

```bash
agent-efficiency doctor
agent-efficiency smoke-test claude
agent-efficiency smoke-test cursor
agent-efficiency smoke-test codex
```

`doctor` checks package metadata, hook manifests, supported capabilities,
storage, policy data, and the privacy boundary. Smoke tests pass fixture events
through the packaged host command. They do not replace a live host session.

The fetch-before-branch signal proves only that the current session observed a
fetch. It does not prove that the remote was current.

## Inspect guidance

The bundled guidance pack is deterministic, project-owned, and available
offline:

```bash
agent-efficiency knowledge status
agent-efficiency knowledge explain core.work-packet
```

Running sessions pin the exact pack digest they started with. Runtime hooks do
not fetch guidance or send project data to a remote service.

## Reports

```bash
agent-efficiency status
agent-efficiency report --days 30
agent-efficiency report --session SESSION_ID
agent-efficiency explain last
```

Reports can include:

- tool failures and failure rate;
- repeated actions and broad scans;
- edited turns followed by validation;
- subagent and compaction counts;
- guidance and intervention counts;
- local runtime percentiles;
- exact Claude cost and token totals when status-line input is configured.

Reports describe observed activity. They do not claim that an intervention
caused an outcome or saved money.

## Team experiments

Agent Efficiency can compare matched `observe` and `advise` sessions. Enroll
before the first task event:

```bash
agent-efficiency experiment enroll \
  --id team-pilot \
  --cohort observe \
  --task-class code-review \
  --task-set-digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --profile default-tools \
  --blinded
```

After the task, record structured acceptance evidence and evaluate matched
cohorts:

```bash
agent-efficiency experiment outcome \
  --accepted \
  --evidence test \
  --evidence-digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --completion-evidence current \
  --wall-minutes 42 \
  --corrections 1 \
  --review-minutes 8

agent-efficiency experiment evaluate --id team-pilot --json
```

Experiment records contain controlled labels, counts, durations, states, and
one-way digests. They do not contain task descriptions, evidence content,
paths, prompts, code, outputs, or transcripts.

See [the measurement protocol](docs/measurement.md).

## Privacy

Agent Efficiency stores local operational metadata in SQLite. It does not
persist:

- prompt bodies;
- source code or patches;
- raw commands;
- tool output;
- assistant messages;
- transcripts;
- environment values.

Project basenames, host names, model names, timestamps, counters, classifications,
and one-way fingerprints can still be sensitive operational data. Protect the
database with normal developer filesystem permissions.

See [SECURITY.md](SECURITY.md) for the complete security and privacy model.

## Develop

```bash
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m unittest discover -s tests -v
python scripts/build_policy_pack.py --check
python scripts/build_guidance_pack.py --check
python scripts/validate_distribution.py
python -m agent_efficiency doctor
```

When available:

```bash
claude plugin validate ./plugins/agent-efficiency --strict
```

The repository also includes fixture tests for all declared Claude Code,
Cursor, and Codex events.

## Contribute

Contributions are welcome. Start with
[CONTRIBUTING.md](CONTRIBUTING.md), review the
[Code of Conduct](CODE_OF_CONDUCT.md), and open an issue before a large
behavior or data-contract change.

Good contributions include:

- host event fixtures;
- privacy tests;
- false-positive reductions;
- new verification runners;
- measured policy improvements;
- documentation for real agent workflows.

## Limits

- Hook events are useful observations, not complete billing or security
  boundaries.
- Deterministic heuristics can be wrong.
- Host lifecycle APIs expose different events and effects.
- Model routing is guidance only. Agent Efficiency does not switch models
  behind the user's back.
- Savings require a comparable baseline and accepted-task evidence.
- Hooks still start briefly in `off` mode so an in-session `on` control can
  be recognized.

## License

Agent Efficiency is licensed under the [MIT License](LICENSE).
