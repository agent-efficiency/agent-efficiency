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
- treats monitoring errors as non-blocking;
- caps and deduplicates guidance;
- requires project consent before guard behavior.

Project-defined verification commands are explicit argument arrays. They are
not passed through a shell.

## Stored data

The local SQLite database can contain:

- project basenames and one-way project keys;
- host and model names;
- event times and controlled event classes;
- action fingerprints;
- counts and durations;
- verification receipt digests;
- structured experiment labels and outcomes;
- optional host-provided token and cost totals.

It does not contain:

- prompt bodies;
- raw commands;
- source code or patches;
- tool output;
- assistant messages;
- transcript paths or content;
- environment values;
- verification output;
- experiment task descriptions or evidence content.

Operational metadata can still be sensitive. Protect the database with normal
developer filesystem permissions. Do not commit it.

## Guidance integrity

The bundled guidance pack is project-owned canonical JSON. Validation checks
its shape, card digests, retrieval index, safety fields, and maximum rendered
size.

Each session pins an exact pack digest. Runtime hooks do not download guidance
or activate external content.

## Verification receipts

A passing receipt is evidence only for the exact workspace and check
configuration it binds. A later relevant change makes it stale. Unreadable or
non-Git workspace state is inconclusive.

Receipts do not prove that a check is sufficient for the product risk. Project
maintainers remain responsible for choosing meaningful checks.

## Supported releases

Security fixes are applied to the current default branch. Maintainers may ask
reporters to verify a fix against a private patch before disclosure.
