# Agent Efficiency contributor instructions

## Product contract

Agent Efficiency is a local verification and efficiency governor for coding
agents.

Preserve these invariants:

- Optimize cost to a verified accepted result, not token count alone.
- Never reduce requested scope, security, evidence, or validation to save cost.
- Keep lifecycle hooks local, deterministic, fast, and fail-open.
- Do not persist prompts, code, raw commands, outputs, assistant messages,
  transcripts, or environment values.
- Keep vault note text, note ids, titles, hooks, vault paths, remotes, and
  branches out of the database. Vault receipts hold counts, digests, and fixed
  codes only.
- Keep vault context inside its fixed allowance, and never let a vault failure
  block a session.
- Keep guidance short, sparse, reversible, and measurable.
- Require project consent for guard behavior.
- Report observed activity. Do not claim savings without a comparable baseline.
- Keep Claude Code, Cursor, and Codex behavior semantically aligned.

## Engineering

- Support Python 3.11 and later.
- Keep the runtime standard-library-only.
- Keep installed assets under `plugins/agent-efficiency/`.
- Use SQLite transactions for concurrent hook processes.
- Add deterministic fixtures for host event changes.
- Bind verification receipts to workspace and configuration state.
- Keep each host's hook file named in its own manifest. Do not ship
  `hooks/hooks.json`.
- Build bundled policies with `scripts/build_policy_pack.py`.
- Build bundled guidance with `scripts/build_guidance_pack.py`.
- Do not add external source trees, generated feeds, or mixed-license content.

## Validation

Run:

```bash
python -m unittest discover -s tests -v
python scripts/build_policy_pack.py --check
python scripts/build_guidance_pack.py --check
python scripts/validate_distribution.py
python -m agent_efficiency doctor --json
```

Also run host plugin validators when they are available.
