# Reports

This guide helps you read what Agent Efficiency recorded about your sessions,
and run a fair comparison of `observe` and `advise` sessions.

## Commands

```bash
agent-efficiency status
agent-efficiency report --days 30
agent-efficiency report --session SESSION_ID
agent-efficiency explain last
```

- `status` shows the latest session in the current folder, or the latest
  session anywhere when none started there.
- `report` sums every session in the last N days, or one session.
- `explain last` shows the most recent note the plugin added, and what
  happened after it.

Add `--json` to any of them for machine-readable output. In a session, send
`$agent-efficiency status` as a chat message to see the same line as `status`.

## Read a report

This report comes from one short session. The agent's test run failed twice,
the agent edited `calc.py`, and then ran the tests again:

```text
Agent Efficiency report: last 30 days
Sessions 1 | turns 1 | tools 8 | nudges 3
Failures 2 (25.0% of observed tools) | repeated-action signals 1 | broad scans 0
Edited turns verified 1/1 (100.0%)
Subagents 0 | compactions 0 | validation calls 1
Host-reported token and cost metrics: unavailable (lifecycle counts are still valid).
Modes: advise=1 sessions
Capability guidance: 0 envelopes | 0 context characters | 0 utility ratings
Measured local layer runtime: selection path p95 13.3130 ms (1 samples) | startup p95 80.4190 ms (1 samples)
Full hook runtime: normal-hook p95 205.3450 ms (13 samples) | session-start p95 264.6540 ms (1 samples)
Guidance expansion gate: keep source set closed (0/20 ratings)
Interventions: emitted=3
Observed association rates: followed=66.7% | disabled=0.0% | false or unnecessary=not enough data
Claim boundary: observed results are associated with interventions. They do not establish causation or savings.
```

| Line | What it counts |
| --- | --- |
| `Sessions`, `turns`, `tools`, `nudges` | Sessions, user prompts, tool events, and notes the plugin added. |
| `Failures` | Tool calls that failed, and their share of all tool events. |
| `repeated-action signals`, `broad scans` | Times an identical action repeated, and searches of the whole disk or home folder. |
| `Edited turns verified` | Turns with a code edit that were followed by a validation command. |
| `Subagents`, `compactions`, `validation calls` | Subagents started, context compactions, and test, lint, type check, or build commands. |
| `Host-reported ...` | Cost and token totals. They come only from the Claude Code status line; see [Status line](install.md#status-line-for-claude-code). |
| `Modes` | Sessions per mode. |
| `Capability guidance` | Guidance cards added, their size, and your ratings of them. |
| `Measured local layer runtime`, `Full hook runtime` | How long the plugin's own work took, as the 95th percentile (p95). |
| `Guidance expansion gate` | Whether enough ratings exist to consider more guidance sources. |
| `Interventions` | Notes by what happened to each one, such as `emitted`. |
| `Observed association rates` | Of the notes shown: how often the next action matched the note, how often you switched to `observe` or `off` after one, and how many of your rated notes you rated false or unnecessary. |
| `Vault context` | When a vault is registered: blocks selected, sent, deferred, unavailable, skipped, withheld, and degraded. |

A rate is shown only when there is something to divide by. Otherwise the line
says `not enough data` or `unavailable`. The runtime numbers differ on every
machine and every run.

## The claim boundary

Every report ends with the same line:

```text
Claim boundary: observed results are associated with interventions. They do not establish causation or savings.
```

A report says what happened after a note. It cannot say that the note caused
it, or that it saved money or tokens. To make that kind of claim, you need a
comparison with a baseline. See [Experiments](#experiments).

## Rate a note

`explain last` shows the latest note and what followed it:

```text
Intervention 3: core.verify-change revision 1
Observed fact class: unverified_change
Requested effect: add_context
Rendered effect: add_context via add_context
Host disposition: emitted
Observed next results: turn_completed, check_started, suggested_action_observed, check_passed, failure_repeated
Current evidence: project=inconclusive
Human judgment: not rated
Claim boundary: The next events are associated observations. They do not prove that the intervention caused the result.
```

Record your judgment of it once:

```bash
agent-efficiency explain last --rate useful
```

The choices are `useful`, `neutral`, and `false_or_unnecessary`. The
`false or unnecessary` rate in reports comes from these ratings.

## Experiments

An experiment compares matched `observe` and `advise` sessions on the same
kind of task. Enroll a session before its first task event:

```bash
agent-efficiency experiment enroll \
  --id team-pilot \
  --cohort observe \
  --task-class code-review \
  --task-set-digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --profile default-tools \
  --blinded
```

After the task, record structured evidence that it was accepted, then compare
the cohorts:

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

Sessions are compared only within the same experiment, task class, project,
task set, host, model, and agent profile. Experiment records hold labels,
counts, durations, states, and one-way digests. They never hold task
descriptions, evidence content, paths, prompts, code, output, or transcripts.

The full method is in [the measurement protocol](measurement.md).
