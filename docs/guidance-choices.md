# User choices for optional guidance

This unreleased slice turns a prepared local suggestion into an explicit user
choice. It supports one action: start a search in a known component, then
broaden the search when needed. It does not draft suggestions automatically,
infer preferences, or measure savings.

The complete proposed improvement loop remains in
[the illustrated guide](how-it-works.html#improvement-loop).

## What the user sees

A matching request can show an **Agent Efficiency suggestion**. It includes:

- The proposed search location and why it may help.
- The note reference supplied by the proposal author.
- The words that matched the request and the scope of future use.
- A statement that savings have not been measured.
- Four exact commands for the user to choose from.

The notice does not stop the normal request. It does not add the proposal as
agent instructions. Ignoring it makes no change. An accepted tip takes priority
over a new suggestion. At most one tip or new suggestion is returned per request.

| Choice | Behavior |
| --- | --- |
| Use once | Add the tip to the request that prompted the notice. Do not reuse it automatically. |
| Use for similar tasks | Add it now and on later requests matching every listed term in the same working directory. |
| Not now | Apply nothing. Wait at least 24 hours before offering the search location on another request. |
| Don't suggest again | Apply nothing. Suppress this search location in the working directory, including proposals with revised titles, reasons, or matching terms. |

An unanswered notice also has a 24-hour cooldown. These limits apply across
sessions in the same working directory. Separate worktrees have separate scope.
Every ordinary user prompt starts a new request for this slice. Guidance control
prompts do not start requests. Use-once consent does not carry across normal
follow-up prompts.

## Prepare a suggestion

Create `agent-efficiency-guidance.json` in the working directory where the host
starts. The file is optional. Existing vault notes remain unchanged. A user or
agent can prepare this file from a saved note, but preparing it never accepts it.
Do not put private notes in a tracked file unless that repository is an intended
destination for them.

For a project containing `src/auth`, this example proposes a narrower first search:

```json
{
  "schema": 1,
  "suggestions": [
    {
      "kind": "search-start",
      "target": "src/auth",
      "terms": ["auth"],
      "title": "Start auth searches in its component",
      "reason": "The saved project note identifies this auth component.",
      "source": "Project note: auth layout",
      "expires": "2026-12-07"
    }
  ]
}
```

Choose an expiry date appropriate to the source. A proposal is inactive on and
after that date. The `source` and `reason` are author-supplied explanations.
Hooks do not verify the source note or claim to have observed redundant work.

Validate the file with the CLI from this checkout or an installation of this
unreleased change:

```sh
agent-efficiency guidance validate --cwd /path/to/project
```

Validation reads the file without changing choices or opening the runtime
database. It reports the count of unexpired suggestions. There is no CLI command
for accepting guidance.

The format is deliberately narrow:

- At most 20 suggestions and 32,768 bytes per file. Unknown fields are errors.
- One to eight distinct lowercase ASCII words in `terms`. Every word must match
  a whole word in the request. Matching ignores case.
- The target must be an existing directory inside the working directory.
  Parent paths and links outside it are rejected. Duplicate locations are errors.
- Title, reason, source, and target limits are 80, 240, 160, and 120 characters.
- The file is read only from the current working directory. Parent directories
  are not searched. Invalid files disable optional suggestions for that event.

## Choose in the host

Send one exact command from the notice as a user prompt. Each includes a token
that binds the choice to the offered version, session, request, and directory.
Tokens expire after 24 hours. An edited proposal, later ordinary prompt, different
session, or already used token cannot activate the old offer.

If the host has ended the request, use once applies nothing. Use for similar
tasks can still adopt the tip for future requests without restarting ended work.

Acceptance adds a short instruction and a visible confirmation. Deferral and
rejection consume only the explicit control prompt. They do not start an agent
task. The acceptance command asks the agent to continue the request that prompted
the suggestion; whether it follows the tip still depends on the agent.

To revoke future adoption and clear rejection choices for this directory, send:

```text
agent-efficiency guidance reset
```

Reset keeps the receipt history and invalidates pending tokens. Cooldowns still
apply. Text already in the conversation cannot be removed by the plugin. Start
a new session to clear earlier context. Reset also works while the session is off.

The trusted input is the host's user-prompt event. Tool calls and assistant text
cannot choose guidance through the normal dispatcher. This is a workflow boundary,
not isolation from a process that can edit the database or forge host input.

## Host support and context limits

Claude Code and Codex use the documented `systemMessage` field at
`UserPromptSubmit`. It carries the notice without setting `continue: false`.
See [Claude hook output](https://code.claude.com/docs/en/hooks#json-output) and
[Codex common output fields](https://learn.chatgpt.com/docs/hooks#common-output-fields).

Cursor's prompt hook documents `user_message` only for blocked prompts. This
slice therefore neither offers nor applies these suggestions in Cursor. It does
not substitute hidden model context for a visible notice. See
[Cursor beforeSubmitPrompt](https://cursor.com/docs/hooks#beforesubmitprompt).

Fixtures check the host output contracts. They do not prove that every installed
host version shows the notice. Interactive host display and command routing still
need a live acceptance check before release.

Suggestions run only in advise or guard mode and are disabled in enrolled
experiments. Each applied tip is at most 500 characters. When a proposal file is
present and active on a supported host, vault rendering reserves 500 characters
inside its existing 9,000-character allowance. Later reloads also reserve space
for tips already returned. The standalone `vault show` preview shows the base
selection before this optional reservation. If a file
appears after a full vault block was loaded, a tip that would exceed the allowance
is withheld and that reason is recorded. A use-once choice then ends; a similar
choice remains adopted for later matching requests.

Required verification, security checks, and task scope still apply. Guidance can
change where a search starts. It cannot authorize skipping work.

## Evidence and remaining work

Three optional SQLite tables have the suffix `_v1`: offers, current choices, and
events. They are additive to data schema 8. Older clients ignore them. Their
creation and choice transitions use transactions. Proposal text, note references,
targets, and matching terms remain out of the database. The tables hold digests,
session identifiers, timestamps, closed choice codes, and character counts.

`notice_prepared` records that the hook prepared a notice for output. It does not assert
that the host rendered it or that a person read it. `context_prepared` records
that accepted guidance was prepared for output. It does not assert that the host
received it or the agent followed it. No receipt asserts task acceptance or token
savings. A failed host delivery may lose a notice or a one-time tip. The plugin
does not retry automatically.

Remaining work includes live host acceptance, Cursor delivery, automatic proposal
drafting from notes and corrections, management of individual adopted tips,
feedback on stale advice, and outcome evaluation. Drafting cost and actual model
token totals are unknown in this slice. No Ambara dependency or source code was
added.
