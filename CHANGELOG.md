# Changelog

## 0.2.0 - 2026-09-29

### Update now if you use Claude Code

Version 0.1.0 shipped a default `hooks/hooks.json` file. Claude Code loads that
file as well as the hook file its manifest names, so the Codex hook commands
also ran under Claude Code and failed on every event. Version 0.2.0 removes the
file, and each host manifest now names its own hook file. Claude Code keeps its
cached plugin folder until the version changes, so update the plugin to pick up
the fix. `agent-efficiency doctor` reports Claude Code as not ready while a
stale `hooks/hooks.json` remains.

### Added: vault context

A vault is a git repository of short markdown notes about your own work.
Agent Efficiency can now add a bounded block of those notes to each session.

- `vault init` creates a tree with one classification: core, work, or
  private.
- `vault index` rebuilds the tree's `index.json` and `INDEX.md`.
- `vault check` and the installed pre-commit and pre-push hooks check
  classification, size caps, note schema, duplicate ids, the index, known
  credential shapes, and the push remote.
- `vault register` adds a tree to the local list of trees sessions read.
- `vault show` prints exactly what a session in a directory would receive.
- `vault migrate propose` and `vault migrate apply` move an existing folder of
  memory notes into vault trees in two steps, with a proposal you edit first.
  If any step of the apply fails, every tree it touched is restored, index
  files included.
- The project note for the working directory is chosen by `paths`, `repos`,
  and `branches`, from git metadata files, without running git.
- The whole block fits a 9,000 character allowance, and anything left out is
  counted in a closing line.
- `$agent-efficiency vault` reloads vault context in the current session.
- Reports count vault context selected, emitted, deferred, and unavailable.
- The database records one receipt per delivery decision with counts, two
  digests, and fixed codes. It never records note text, note ids, titles,
  hooks, vault paths, remotes, or branches.

### Vault delivery on each host

- Claude Code and Codex receive vault context at session start. After each
  compaction they receive it once, at whichever event comes first.
- Cursor receives vault context at session start. Cursor cloud agents send no
  session start, so it arrives with the first successful tool result.
- Cursor sends no event after a compaction that can carry context, so the next
  successful tool result after each `preCompact` restores vault context once.
- A Cursor prompt cannot carry context, so `$agent-efficiency vault` on Cursor
  reloads at the next successful tool result, once.

## 0.1.0 - 2026-08-26

First public release.

- Local verification and efficiency governance for Claude Code, Cursor, and
  Codex, with a shared runtime that uses only the Python standard library.
- Detection of repeated work, repeated failures, broad scans, and unverified
  code changes, with sparse guidance in `advise` mode.
- Project-defined checks with receipts bound to the exact Git and
  verification state.
- Reports of observed activity and matched `observe` and `advise` experiments.
- No storage of prompts, source code, raw commands, outputs, or transcripts.
