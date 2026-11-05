---
name: agent-efficiency
description: Operate the local Agent Efficiency governor when the user explicitly asks to enable, disable, observe, inspect, report on, tune, or measure coding-agent quality and efficiency, or invokes $agent-efficiency. Use for verification evidence, bundled guidance, and efficiency experiments. Do not invoke for ordinary coding work because lifecycle hooks monitor it automatically.
---

# Agent Efficiency

Optimize total cost to a verified accepted outcome. Token count is only one
input: include monitor overhead, failed attempts, rework, review burden, and
defect risk.

## Session controls

Prefer the in-session controls below. The user types one as an ordinary chat
message; it is not a slash command. The prompt hook answers an exact control
phrase before a model call, so changing modes does not spend a model turn:

```text
$agent-efficiency on
$agent-efficiency observe
$agent-efficiency guard
$agent-efficiency off
$agent-efficiency status
$agent-efficiency vault
$agent-efficiency help
```

`vault` reloads vault context for the current folder.

`on` is an alias for `advise`. Modes are session-scoped:

- `off`: no telemetry and no guidance, except processing a later control.
- `observe`: record privacy-preserving counters without adding agent context.
- `advise`: observe and emit sparse, deduplicated guidance.
- `guard`: retain advice and continue a stopped turn once when a current
  required receipt is absent. This works only when the repository config also
  sets `mode = "guard"`.

Use `agent-efficiency default MODE` only when the user asks to change the
default for future sessions. `agent-efficiency mode MODE` changes the most
recent session whose working folder is the current folder.

## The command

For every CLI example below, use `agent-efficiency` when it is on `PATH`. In a
Claude Code session the plugin's `bin` folder is usually on `PATH`. Otherwise
run the plugin's own `<plugin-root>/bin/agent-efficiency` wrapper; resolve
`<plugin-root>` from this skill's installed location. Every copy of the command
and every host use the same data folder, so either copy sees the same sessions.

The user's own terminal and the vault git hooks need the command installed
there. The supported way is `pipx install
git+https://github.com/agent-efficiency/agent-efficiency@vVERSION`, where
VERSION is what `agent-efficiency --version` prints. The install guide has the
details:
https://github.com/agent-efficiency/agent-efficiency/blob/main/docs/install.md.
Tell the user about it when a terminal step needs the command. Do not install
it yourself unless the user asks.

## Operating contract

When guidance is active:

1. Scale planning to ambiguity and reversal cost. Do not plan mechanical work
   for ceremony's sake.
2. For substantial work, establish acceptance checks, boundaries, and the
   smallest end-to-end slice before implementation.
3. Reuse repository instructions, existing patterns, and current state before
   generating new abstractions.
4. Treat repeated commands, repeated failures, broad scans, and unnecessary
   delegation as signals to stop and change approach.
5. Verify changed behavior at the relevant boundary before claiming completion.
6. Never reduce requirements, security, evidence, or validation to save cost.

Runtime policies are advisory except for one bounded completion checkpoint:
after code changes with no observed validation, the Stop hook can ask the same
agent to validate once in that turn. It never loops a second time.

## Inspect and report

Use the local CLI:

```bash
agent-efficiency status
agent-efficiency report
agent-efficiency doctor
agent-efficiency knowledge status
agent-efficiency knowledge explain CARD_ID
```

Describe observed counts and exact host metrics when available. Never claim
money or tokens were saved without a comparable baseline. Claude Code's
optional status-line adapter can provide exact session cost and context usage;
Codex hook data is operational telemetry, not a billing record.

`knowledge status` and `knowledge explain` are offline. Use them to inspect the
bundled pack, the current session pin, selection receipts, principles,
reviewers, and expiry.

## Capability pack

Inspect the installed offline pack with:

```bash
agent-efficiency knowledge status
agent-efficiency knowledge explain CARD_ID
```

The pack contains project-owned guidance. Runtime hooks never fetch guidance
or send project data to a remote service.

## Tune carefully

Change thresholds only after reviewing reports across multiple sessions.
Prefer fewer, higher-value nudges. If the monitor produces noise or measurably
adds more context than it saves, move the policy to `observe`, narrow its
trigger, or retire it.
