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
