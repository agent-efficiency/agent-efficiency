# Measurement protocol

Agent Efficiency should earn its own overhead. Measure that rather than
assuming it.

## Primary outcome

Compare the total cost required to reach a verified accepted outcome, not the
cost of the first answer.

For matched tasks, capture:

- host-reported model cost and tokens when available
- wall-clock time to acceptance
- failed tool calls and repeated-action signals
- number of user correction turns
- edited turns with observed validation
- escaped defects or review findings
- human review minutes

## First experiment

1. Keep the agent host, model, effort, repository, and task class stable.
2. Before each task, enroll its fresh session in the named `observe` or
   `advise` cohort.
3. Use the same enrollment rule and comparable task source for both periods.
4. Exclude sessions with materially different scope or unavailable acceptance
   evidence.
5. Compare medians and inspect outliers; do not rely on one aggregate score.
6. Read every emitted nudge. Mark capability cards useful, neutral, or
   distracting.
7. Rate the last policy intervention as useful, neutral, or false or
   unnecessary.
8. Declare completion evidence as current, stale, missing, failed, blocked, or
   inconclusive.

Use the local experiment ledger:

```bash
agent-efficiency experiment enroll \
  --id team-pilot \
  --cohort observe \
  --task-class security-review \
  --task-set-digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --profile sonnet-high-default-tools \
  --blinded
agent-efficiency experiment status
agent-efficiency experiment rate core.narrow-review useful \
  --changed-next-action
agent-efficiency experiment outcome \
  --accepted \
  --evidence test \
  --evidence-digest sha256:0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef \
  --completion-evidence current \
  --wall-minutes 42
agent-efficiency experiment evaluate --id team-pilot --json
```

Run ledger commands from a separate terminal rather than asking the measured
agent to execute them. Add `--session SESSION_ID` when needed. Otherwise the
bookkeeping itself becomes part of the task's tool and correction counts.
Use `experiment invalidate --reason REASON` with `data-entry-error`,
`protocol-deviation`, or `evidence-withdrawn` to append a permanent exclusion
without deleting or rewriting the original evidence.

Enrollment, outcomes, and ratings are immutable. Use a new session instead of
rewriting an earlier cohort or judgment. Accepted outcomes with evidence
`none` remain visible but are excluded from comparison. A post-enrollment mode
change is also retained as an invalidation and excluded; turning telemetry off
is always allowed.
Enrollment is rejected once the session records a task turn, tool, or nudge.
Assign it immediately after `SessionStart`, before providing the task.
The task-set digest must bind the exact preregistered external task manifest
shared by both cohorts; the manifest itself is not stored.
Every non-`none` evidence kind requires a SHA-256 reference to the external
acceptance artifact or receipt. The local ledger does not store its bytes,
path, URL, or output.
Declare `--blinded` only when relevance is judged without revealing the
expected card or cohort result first. Use `--not-blinded` for ordinary live
feedback. Both remain reportable, but only valid predeclared blinded judgments
count toward guidance expansion.

At minimum, compare:

```text
cost per accepted task
failure calls per 100 tool calls
verified edited turns / edited turns
user correction turns per task
```

## Pilot gate

Run at least 24 matched tasks across Claude Code, Cursor, and Codex. The
experiment evaluator reports the pilot gate in JSON. It requires no reduction
in verified acceptance, plus either a 30% relative reduction in stale or
missing completion evidence or a 15% reduction in median actions. False or
unnecessary interventions must remain below 10%. Median action overhead must
remain below 5%. Both full-hook runtime percentile gates must
pass. Every emitted advice intervention must be rated before the pilot can
pass.

If evidence freshness and action count both fail to improve, stop adding
guidance. A pilot result supports a product decision only. It does not establish
causation.

## Decision rule

Keep a policy active when it reduces rework, cost, or review burden without
lowering acceptance quality. Narrow or retire it when it fires often but does
not change the next action, causes unnecessary work, or adds more context than
it saves.

Do not claim "tokens saved" from avoided-action heuristics. A repeat signal is
an opportunity; only a controlled comparison can estimate an effect.

The evaluator opens its cost-claim eligibility flag only when an exact matched
stratum has at least five verified accepted tasks in each cohort and every
cohort has the same valid enrollment count. Every valid enrolled session must
have a recorded outcome and host-reported cost. Evidence digests must also be
unique per accepted task. Cohort cost, failures, review, context, and runtime
are divided by verified accepted tasks, and acceptance rate remains explicit,
so failed work cannot disappear from the denominator. The evaluator reports
`advise minus observe` component differences and does not infer causality.

Context characters and measured selector/session-start runtime are part of the
report. Review time, defects, failures, wall time, and dollars remain separate
units; the tool does not hide tradeoffs inside an invented weighted score.
The local selection timer includes pack lookup, selection, and its receipt
transaction. Timing ends before the final timing-sample insert,
so the reported runtime is a documented lower bound.

Do not expand bundled guidance until the existing cards have at least 20
ratings, at least 80% useful, and no more than 5% distracting. That result is
permission to review an expansion, not proof that a new card will help.
