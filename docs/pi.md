# Pi integration (experimental, observe-only)

This checkout includes a Pi extension backed by the shared Python runtime.
It is not yet a supported release host. No npm or Python installation is needed;
Python 3.11+ must be available as `python3`.

## Try it

From the repository root:

```sh
pi -e ./plugins/agent-efficiency/pi/index.ts
```

For an existing session, add an extension loader to
`~/.pi/agent/extensions/agent-efficiency.ts` pointing at this checkout:

```ts
export { default } from '/absolute/path/to/agent-efficiency/plugins/agent-efficiency/pi/core.mjs';
```

Then run `/reload` in Pi. This global loader applies to new Pi sessions too.
Remove that loader and reload to uninstall; local evidence is not deleted.

Commands:

- `/efficiency status` or `/efficiency report`: persistent panel with mode,
  observed prompt/tool/failure counts, and the latest five advice previews.
- `/efficiency off`: stop monitoring this session.
- `/efficiency observe`: resume silent monitoring.

New sessions default to observe. Reload/resume retain the stored session mode;
forks get a separate session identity. Tree navigation does not rewind telemetry:
counts describe activity that actually occurred, not just the surviving branch.
No old transcript is replayed or imported. Counts start when the extension loads.

## Boundaries

This first version does **not** inject advice, read prompts, load vault context,
run verification commands, claim completion proof, block tools, or continue an
agent turn. Guard mode is unsupported even if project configuration requests it.
There are no model calls or network requests in the bridge.

Operational rules now run in dry-run mode using the shared advisory selectors,
thresholds, priority, and per-turn deduplication/budget. Candidates are counted in
the footer; `/efficiency report` shows the rule, fixed reason, event, UTC time,
and advice text. No advice is sent to the model and nothing is blocked. The
preview count is not a savings metric or a delivered-intervention count.
Candidates are recorded separately from interventions, using only policy codes,
event codes, timestamps, and session/turn identifiers. Advice text is resolved
from the currently installed policy pack when the report is read, not stored.

Examples include repeated identical actions, repeated failures, broad searches,
and an edit without observed validation. Distinct successful reads alone should
not trigger these rules. Earlier events are not replayed; previewing starts when
the updated Python bridge is invoked. The TypeScript UI requires `/reload`.
Guard enforcement is **not simulated**: there is no claim that required checks
are missing or that a completion would have been blocked. A code-edit reminder
is advisory, not a verification receipt. Prompt-based guidance remains unavailable.

Pi `before_agent_start` maps to a prompted turn; individual model/tool iterations
are not counted as additional user turns. Tools are correlated by call ID, including
parallel/nested calls. `tool_execution_end` supplies the finalized error flag, so
blocked and aborted calls can be recorded too. A tool's error flag is not proof
of a test result; background commands that launch successfully cannot establish
that their eventual test run passed. `agent_settled`, not `agent_end`, is the
stop boundary. Aborted runs do not emit a normal stop.

Only tool paths and Bash commands are passed transiently over stdin for runtime
classification and keyed fingerprints. Source edits, prompts, results, images,
and transcripts are not forwarded. Custom-tool arguments are not analyzed.
Prompt-based task classification and cost/token metrics are unavailable.
The runtime's existing metadata storage contract applies (counts, fixed classes,
fingerprints, project basename); raw commands and paths are not saved as events.

Data is isolated under `~/.local/share/agent-efficiency-pi`, not the existing
Claude/Codex/Cursor database. Override with `AGENT_EFFICIENCY_PI_DATA_DIR`.
`AGENT_EFFICIENCY_PI_PYTHON` selects another Python executable. These environment
variables are read by the extension/runtime; they are not recorded as telemetry.

Each bridge request has a 1.5-second deadline, a 64-KiB input limit, and an 8-KiB
output limit. Errors fail open with a fixed warning once per extension load.
They never include exception text or raw work content. Missing monitoring events
are not backfilled. The footer says unavailable when a request fails.

## Validation

```sh
PYTHONPATH=plugins/agent-efficiency/src python3 -m unittest discover -s tests -p test_pi_bridge.py
node --test tests/pi-extension.test.mjs
```

Also run the repository's full validation commands. The existing doctor and
host smoke-test commands still describe the three released hosts, not Pi.
The extension was manually smoke-tested with real Pi RPC startup, command
discovery, and `/efficiency status`, without a model request.

Next stages: opt-in advisory effect delivery, explicit verification checks with project
trust/consent and environment-aware receipts, then separately reviewed bounded
completion guards. Existing project gates remain authoritative.

The [Pi field-opportunity backlog](sprints/2026-11-pi-field-opportunities.md)
records observed prerequisite, background-result and environment gaps, plus
proposed acceptance tests. These are research/design items, not implemented
interventions or evidence of savings.
