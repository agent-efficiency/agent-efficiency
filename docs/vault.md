# Vault

This guide helps you give every agent session your own notes: the current
state of each project and the standing rules you want every session to follow.

A vault is a git repository of short markdown notes that you keep. Agent
Efficiency reads the vaults you register and adds a bounded block of their
notes to each new session, chosen by the session's working folder. It never
writes to a vault during a session.

## Quick start

This example makes two trees (vault repositories): a core tree for rules that
apply everywhere, and a work tree for project notes. Use your own paths and
remotes.

1. Create both trees. `--remote` names the only git remote the tree may be
   pushed to. Without it, the vault's pre-push hook refuses every push.

   ```bash
   agent-efficiency vault init ~/notes/vault-core --classification core \
     --remote git@github.com:example/vault-core.git
   agent-efficiency vault init ~/notes/vault-work --classification work \
     --remote git@github.com:example/vault-work.git
   ```

   ```text
   Vault tree ready at /home/you/notes/vault-core (core)
   Run: git init && git config --local core.hooksPath .githooks
   ```

2. Write a note in each tree. A rule for every session goes in the core tree:

   ```bash
   cat > ~/notes/vault-core/feedback/tests-first.md <<'EOF'
   ---
   schema: 1
   id: tests-first
   title: Tests first
   type: feedback
   classification: core
   status: active
   hook: Run the smallest test that covers a change before you say it is done.
   ---
   Run the test that covers the change. Say which test you ran.
   EOF
   ```

   A project note goes in the work tree. `repos` ties it to a repository:

   ```bash
   cat > ~/notes/vault-work/projects/app.md <<'EOF'
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
   EOF
   ```

3. Index and check both trees. Run this again after every edit to a note.

   ```bash
   for tree in ~/notes/vault-core ~/notes/vault-work; do
     agent-efficiency vault index "$tree"
     agent-efficiency vault check "$tree"
   done
   ```

   ```text
   Index written for /home/you/notes/vault-core
   No findings.
   Index written for /home/you/notes/vault-work
   No findings.
   ```

4. Put each tree under git with its guard hooks turned on, and commit. The
   pre-commit hook runs `vault check` on what you commit.

   ```bash
   for tree in ~/notes/vault-core ~/notes/vault-work; do
     git -C "$tree" init
     git -C "$tree" config --local core.hooksPath .githooks
     git -C "$tree" add -A
     git -C "$tree" commit -m "Start the vault"
   done
   ```

5. Register both trees, so sessions read them:

   ```bash
   agent-efficiency vault register ~/notes/vault-core
   agent-efficiency vault register ~/notes/vault-work
   ```

   ```text
   Registered the core tree at /home/you/notes/vault-core.
   Registered the work tree at /home/you/notes/vault-work.
   ```

6. Preview what a session in a project folder receives. Here `~/code/app` is a
   clone of `example/app`:

   ```bash
   agent-efficiency vault show --cwd ~/code/app
   ```

   See the output in [What a session receives](#what-a-session-receives).

The next session that starts in `~/code/app` gets this block. In a session
that is already running, send `$agent-efficiency vault` as a chat message to
load it now.

## Trees

Each tree has one classification: `core`, `work`, or `private`. Core notes load
in every session. A work or private tree loads only when the working folder
matches one of its project notes. You can register at most one tree per
classification.

`vault init` creates:

- `.vault.json`, the marker that holds the classification and the allowed
  remote;
- the note folders `projects`, `feedback`, `reference`, `doctrine`, and
  `sessions`;
- `index.json` and `INDEX.md`, which list no notes yet;
- the pre-commit and pre-push hooks in `.githooks`.

Git does not keep an empty folder, so a note folder you have not used yet is
not in your commits. `vault init` on a clone creates the missing folders again.
Run `vault init` again on an existing tree to update its hooks after an
upgrade. It never changes the classification or replaces a recorded remote.

## Notes

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

- **Required keys:** `schema` (always `1`), `id`, `title`, `type`,
  `classification`, `status`, and `hook`.
- **`type`:** `project`, `feedback`, `reference`, `doctrine`, or `session`.
- **`status`:** `active`, `dormant`, or `done`. Only active notes reach a
  session. Session notes never do.
- **`hook`:** the one line the note adds to a session, at most 100
  characters.
- **Optional keys:** `updated`, and the lists `repos`, `paths`, `branches`,
  and `links`. A value is one scalar or one bracketed list. Unknown keys are
  refused.
- **Size caps, for the whole file:** 3,000 characters for a project note,
  3,500 for a feedback, reference, or doctrine note, and 1,000 for a session
  note.

## How a project note is chosen

Selection reads each tree's `index.json` and the working folder's git
metadata files. It never runs git and never opens a note body to decide. The
first rule that matches wins:

1. **`paths`:** the longest entry that contains the working folder. An entry
   that starts with `/` or `~` is a folder on disk. Any other entry is relative
   to the repository root and counts only when `repos` also matches.
2. **`repos`:** an entry that matches a remote of the repository. A bare
   `owner/name`, such as `example/app`, matches that repository on any host.
3. **`branches`:** breaks an exact tie. A note that names the current branch
   wins, then a note that names no branch. If a tie remains, the result is
   ambiguous and no project note loads.

## What a session receives

This is the real output of step 6 of the quick start:

```text
Vault context: project state and standing notes from the user's vault. Each line below a heading is a note file in that folder, written without its .md ending. Open the file when its line applies to the task.

## Project: Example app
File: /home/you/notes/vault-work/projects/app.md
Next: finish the settings page, then cut a release.

## Core notes in /home/you/notes/vault-core
- feedback/tests-first: Run the smallest test that covers a change before you say it is done.

Summary: matched | 473 of 9000 characters | head 51 | notes 2 | omitted 0 | reason none
```

The block has these parts, in this order:

1. A fixed header line.
2. The matched project note, in full: its title, its file, and its body.
3. One line per active feedback, reference, and doctrine note, core tree
   first, then the tree that holds the match. Each line is the note's path
   without `.md`, then its hook. The agent opens the file when a line applies.
4. One line per other active project in the same trees.
5. When notes did not fit, a closing line that counts them.

The whole block has an allowance of 9,000 characters. With no match, the block
says so and lists only core notes.

The `Summary` line is printed by `vault show` only. A session does not get it.

| Field | Meaning |
| --- | --- |
| `matched` | How selection ended: `matched`, `unmapped` (no project note matches), or `ambiguous`. |
| `473 of 9000 characters` | The size of the block, and the allowance. |
| `head 51` | Characters of the project note body shown. `0` when no project note loaded. |
| `notes 2` | Notes in the block: the project note plus each one-line note. |
| `omitted 0` | Notes left out to stay inside the allowance. |
| `reason none` | Why the block is not a plain match: `unmapped`, `ambiguous`, `over_budget` (notes were left out), `cap_exceeded` (the project note is over its cap), or `parse_error`. |

## Delivery by host

- **Claude Code and Codex:** at session start. A resumed session gets the
  block again only when it changed. After each compaction, the block is sent
  once at the compact session start. `PostCompact` does not accept context
  and does not consume the pending delivery.
- **Cursor:** at session start. Cursor cloud agents send no session start, so
  the block arrives with the first successful tool result. Cursor sends no
  event after a compaction that can carry context, so after each `preCompact`
  the next successful tool result carries it once.
- **`$agent-efficiency vault`:** selects again and sends the block again.
  Claude Code and Codex get it at once. Cursor gets it with the next
  successful tool result.

A block sent after a compaction or a reload says that it replaces the earlier
vault context. In `observe` mode the selection is recorded and nothing is
added. In `off` mode the vault is not read. A vault failure never blocks a
session. The session gets one line that says why, and
`agent-efficiency vault show` gives the detail.

## The git hooks

The pre-commit hook checks what you are about to commit. The pre-push hook
checks every commit a push would send. Both check:

- that each note's classification matches its tree;
- size caps and the note format;
- duplicate ids;
- that `index.json` matches the notes, so a new note is never left out of
  sessions;
- known credential shapes: private keys, GitHub tokens, AWS access keys, and
  a key such as `token` or `password` set to a long value. A clean result does
  not prove that a tree holds no secret.

The pre-push hook also refuses any remote other than the one in `.vault.json`.
Each finding names a rule, a place, and a fix. It never repeats the secret or
the note text.

The hooks run the `agent-efficiency` command, so it must be on your PATH.
Without it they stop the commit or push and print how to install it. To turn
them off, see [Uninstall](install.md#uninstall).

## Move existing notes into a vault

`vault migrate propose` reads an existing folder of markdown notes and writes
a proposal file with a guessed classification for each note. Edit the file,
then apply it:

```bash
agent-efficiency vault migrate propose --memory-dir ~/notes/old-memory \
  --out proposal.json
agent-efficiency vault migrate apply proposal.json \
  --tree work=$HOME/notes/vault-work --tree private=$HOME/notes/vault-private
```

Each classification in the proposal needs a `--tree`, created first with
`vault init`. The guess is a keyword hint only. Nothing is written until you
apply the proposal. If any step of the apply fails, every tree it touched is
restored.

## Privacy

Note text goes from the vault to the session and nowhere else. The database
records one receipt per delivery decision, with counts, two digests, and fixed
codes. It never records note text, note ids, titles, hooks, vault paths,
remotes, or branches. The list of registered trees is kept in `vault.json` in
the data folder. See [Privacy](privacy.md).

## Troubleshooting

- **A new note never shows up.** Run `agent-efficiency vault index TREE`, then
  commit `index.json` and `INDEX.md`. `vault check` reports `index_stale`
  until you do.
- **`vault show` lists no core notes.** Index and register the core tree too.
- **A push is refused because the tree registers no remote.** Run
  `agent-efficiency vault init TREE --classification CLASS --remote URL` to
  record the remote.
- **A commit fails with `agent-efficiency command is not on PATH`.** Install
  the command. See [Install](install.md#the-terminal-command).
- **`No vault context: unavailable (no_trees)`.** No tree is registered in the
  data folder the command uses. Run `vault register` for each tree.
