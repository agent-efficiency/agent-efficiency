# Security and privacy

## Report a vulnerability

Use GitHub private vulnerability reporting for this repository. Do not open a
public issue for a suspected vulnerability.

Include:

- the affected host and operating system;
- the relevant Agent Efficiency command or lifecycle event;
- a minimal reproduction with private content removed;
- the expected and observed behavior;
- the security or privacy impact.

Do not include credentials, private prompts, source code, production telemetry,
or database files.

## Runtime boundary

Agent Efficiency runs as the current developer user. Hooks do not grant
permissions, rewrite tool input, or execute guidance text.

The lifecycle path:

- makes no network requests;
- makes no model calls;
- uses only the Python standard library;
- reads vault trees only from local files and never writes to them;
- treats monitoring errors as non-blocking;
- caps and deduplicates guidance;
- requires project consent before guard behavior;
- keeps guard on when the project config changes, stops loading, or is
  removed during a session.

Project-defined verification commands are explicit argument arrays. They are
not passed through a shell.

## Stored data

The local SQLite database can contain:

- project basenames and one-way project keys;
- host and model names;
- event times and controlled event classes;
- action fingerprints, keyed with a private local key;
- counts and durations;
- verification receipt digests;
- structured experiment labels and outcomes;
- optional host-provided token and cost totals;
- vault delivery receipts: counts, two digests, and fixed codes.

It does not contain:

- prompt bodies;
- raw commands;
- source code or patches;
- tool output;
- assistant messages;
- transcript paths or content;
- environment values;
- verification output;
- experiment task descriptions or evidence content;
- vault note text, note ids, titles, hooks, vault paths, remotes, or branches.

The runtime data directory also holds `vault.json`, the list of registered
vault tree paths, and `digest.key`, the private key for action fingerprints.
The key is created with mode 0600 on first use. Agent Efficiency never
replaces, repairs, or deletes it. When it is missing and cannot be created,
or is not a private regular file in the expected form, fingerprints use a
fixed built-in key and are as easy to guess as an unkeyed hash; `doctor`
reports this. Vault note text goes only to the host session that receives
it.

Operational metadata can still be sensitive. Protect the database with normal
developer filesystem permissions. Do not commit it.

## Guard mode and the project config

A session that runs in guard pins the digest of the project's
`agent-efficiency.toml` the first time. If the file later changes, stops
loading, or is removed, guard stays on. When the agent stops after a code
change, it is asked once per turn, in fixed text, to tell the user. The
agent cannot accept the change; the user accepts it by typing
`$agent-efficiency guard` in the session. A file that exists but does not
load turns guard on. The pin lasts for one session.

The terminal command `agent-efficiency mode guard` also pins the current
file. An agent with shell access can run it, as it can edit the database:
the store resists mistakes, not deliberate tampering by an agent with shell
access. `doctor` reports whether Claude Code asks before it runs
`agent-efficiency`, `git merge`, and `gh` in a folder, so the choice is
visible. It never changes those settings.

## Host settings

`doctor` reads Claude Code settings files to find a split data folder and
to report permission rules. It treats them as untrusted input: it reads at
most 1 MiB of each, does not follow a link out of the settings folder, and
reports a file it cannot parse as one that could not be read. It never
edits them.

## Guidance integrity

The bundled guidance pack is project-owned canonical JSON. Validation checks
its shape, card digests, retrieval index, safety fields, and maximum rendered
size.

Each session pins an exact pack digest. Runtime hooks do not download guidance
or activate external content.

## Vault guards

The vault pre-commit and pre-push hooks check the content git is about to
record or send, including every commit a push would carry. Findings name a
rule, a location, and a fix. They never repeat matched secret material or note
text. The secret scan matches known credential shapes only, so a clean result
does not prove a tree holds no secret.

## Verification receipts

A passing receipt is evidence only for the exact workspace and check
configuration it binds. A later relevant change makes it stale. Unreadable or
non-Git workspace state is inconclusive.

Receipts do not prove that a check is sufficient for the product risk. Project
maintainers remain responsible for choosing meaningful checks.

## Supported releases

Security fixes are applied to the current default branch. Maintainers may ask
reporters to verify a fix against a private patch before disclosure.
