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
- Loads a bounded block of your own project notes, a vault, into each new
  session, chosen by the working directory.
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
$agent-efficiency vault
```

`on` is an alias for `advise`. `vault` reloads vault context for the current
directory; on Cursor the reload arrives with the next successful tool result.
These controls are intercepted by the hook, so they do not require a model
response.

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

To move an existing install to 0.2.0:

```bash
claude plugin marketplace update agent-efficiency
claude plugin update agent-efficiency@agent-efficiency
```

Claude Code keeps a cached copy of the plugin until its version changes.
Version 0.1.0 shipped a `hooks/hooks.json` file that Claude Code also loaded,
so the Codex hook commands failed there on every event. Version 0.2.0 removes
that file. `agent-efficiency doctor` reports Claude Code as not ready while a
stale copy remains in the plugin folder.

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

The Codex manifest names `plugins/agent-efficiency/hooks/codex-hooks.json`.
Each host manifest names its own hook file. The package ships no
`hooks/hooks.json` because Claude Code loads that default file in addition to
the file its manifest names.

Vault context uses the same install. See
[Load vault context](#load-vault-context).

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

`doctor` checks the package files it runs from and the copy each host
installed: Claude Code and Codex from their install records, and Cursor from
its local plugin folder. A host that is not installed is reported as not
installed, which is not a failure. It also checks hook manifests, supported
capabilities, storage, policy data, and the privacy boundary. Smoke tests pass
fixture events through the packaged host command. They do not replace a live
host session.

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

## Load vault context

A vault is a git repository of short markdown notes that you keep about your
own work: the current state of each project and the standing rules you want
every session to follow. Agent Efficiency reads registered vault trees and
adds a bounded block of their notes to each new session. It never writes to a
vault during a session.

### Create and register a tree

Each tree has one classification: `core`, `work`, or `private`. Core notes load
in every session. A work or private tree loads when the working directory
matches one of its project notes. Register at most one tree per
classification.

```bash
agent-efficiency vault init ~/notes/vault-core --classification core \
  --remote git@github.com:example/vault-core.git
agent-efficiency vault init ~/notes/vault-work --classification work
cd ~/notes/vault-core && git init && git config --local core.hooksPath .githooks
```

`vault init` writes a `.vault.json` marker, the note folders (`projects`,
`feedback`, `reference`, `doctrine`, `sessions`), an empty index, and
pre-commit and pre-push hooks in `.githooks`. Run the same `git` commands in
each tree. The hooks check what git is about to commit or push: note
classification, size caps, note schema, duplicate ids, the index, and known
credential shapes. The pre-push hook also refuses a remote other than the one
the marker names.

A note is markdown with a small frontmatter block:

```markdown
---
schema: 1
id: app
title: Example app
type: project
classification: work
status: active
hook: Current state and next step for example/app.
repos: [example/app]
---
Next: finish the settings page, then cut a release.
```

`hook` is the one line the note adds to a session, at most 100 characters.
A project note is capped at 3,000 characters and a feedback, reference, or
doctrine note at 3,500. Frontmatter values are one scalar or one bracketed
list. Unknown keys are refused.

After you edit notes, rebuild the index and check the tree, then register it:

```bash
agent-efficiency vault index ~/notes/vault-work
agent-efficiency vault check ~/notes/vault-work
agent-efficiency vault register ~/notes/vault-core
agent-efficiency vault register ~/notes/vault-work
```

Registered tree paths are kept in `vault.json` in the runtime data directory,
not in the database.

### What a session receives

Selection reads each tree's generated `index.json` and the working directory's
git metadata. It never runs git and never opens a note body to decide. The
first match wins:

1. The longest `paths` entry that contains the working directory.
2. A `repos` entry that matches a remote of the repository, such as
   `example/app`. A bare `owner/name` matches on any host.
3. `branches` breaks an exact tie. A tie that remains is reported as
   ambiguous and no project note loads.

The matched project note renders in full. The active feedback, reference, and
doctrine notes and the other active projects of the core tree, and of the tree
that holds the match, render as one line each: the note path and its hook. The
agent opens the file when a line applies. Core notes come first. With no
match, only core notes are listed.

The whole block has an allowance of 9,000 characters. Notes that do not fit
are counted in a closing line, so an omission is visible.

Preview exactly what a session in a directory would receive:

```bash
agent-efficiency vault show --cwd ~/code/app
```

Delivery by host:

- Claude Code and Codex: at session start. Resume delivers again only when the
  rendered text changed. After each compaction, it delivers once, at the
  compact session start or at `PostCompact`, whichever comes first.
- Cursor: at session start. Cursor cloud agents send no session start, so the
  context arrives with the first successful tool result. Cursor sends no event
  after a compaction that can carry context, so after each `preCompact` the
  next successful tool result delivers it once.
- `$agent-efficiency vault` reselects and delivers again. Claude Code and
  Codex receive it at once. Cursor cannot add context from a prompt, so the
  reload arrives with the next successful tool result, once.

A delivery after a compaction or a reload is marked as replacing earlier vault
context.

In `observe` mode the selection is recorded and nothing is added. In `off`
mode the vault is not read. A vault failure never blocks the session. When
context is unavailable, the session gets one line saying why, and
`agent-efficiency vault show` gives the detail.

### Move existing notes into a vault

`vault migrate propose` reads an existing folder of markdown memory notes and
writes a proposal file with a guessed classification for each note. Edit the
file, then apply it:

```bash
agent-efficiency vault migrate propose --memory-dir ~/notes/old-memory \
  --out proposal.json
agent-efficiency vault migrate apply proposal.json \
  --tree work=$HOME/notes/vault-work --tree private=$HOME/notes/vault-private
```

Each classification used in the proposal needs a `--tree`, created first with
`vault init`. The guess is a keyword hint only. Nothing is written until you
apply the proposal. If any step of the apply fails, including rebuilding an
index, every tree it touched is restored to what it was, index files included.

### Vault privacy

Note text goes from the vault to the host output and nowhere else. The
database records one receipt per delivery decision with counts, two digests,
and fixed codes. It never records note text, note ids, titles, hooks, vault
paths, remotes, or branches. Reports show vault counts only.

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
- vault context selected, emitted, deferred, and unavailable counts;
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
- environment values;
- vault note text or vault paths.

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
