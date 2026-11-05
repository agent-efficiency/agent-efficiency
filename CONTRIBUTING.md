# Contributing

Agent Efficiency welcomes bug reports, host fixtures, documentation, privacy
tests, policy improvements, and verification integrations.

## Before you start

Open an issue before a large behavior, storage, host contract, or data contract
change. Describe:

- the user problem;
- the supported host or workflow;
- the proposed behavior;
- the privacy impact;
- how the change can be tested;
- what would cause the proposal to be rejected.

Small fixes can go directly to a pull request.

## Development setup

[docs/development.md](docs/development.md) covers setup, checks, trying a
checkout in a host, and the project's limits. In short:

```bash
git clone https://github.com/agent-efficiency/agent-efficiency.git
cd agent-efficiency
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
python -m pip install pre-commit
pre-commit install --install-hooks --hook-type pre-commit --hook-type commit-msg
```

The hooks check whitespace, YAML, JSON, and TOML files, run `ruff check` and
`ruff format --check`, scan for secrets with gitleaks, lint workflow files with
actionlint, and check commit messages. CI runs the same hooks, checks the
message of every commit in the pull request, and scans the full history for
secrets, so a change that skips them locally still fails in the pull request.
Some hooks download Go tools on first run.

Run the full local checks:

```bash
pre-commit run --all-files
python -m unittest discover -s tests -v
python scripts/build_policy_pack.py --check
python scripts/build_guidance_pack.py --check
python scripts/validate_distribution.py
python scripts/check_wheel_install.py
python -m agent_efficiency doctor --json
```

## Pull requests

Keep pull requests focused on one concern. Put formatting-only changes in
their own pull request. Fill in the pull request template. Include:

- the problem and behavior change;
- affected hosts;
- privacy and storage impact;
- tests and fixture evidence;
- known limits;
- documentation changes.

Do not include prompts, private repositories, source code from another
project, production telemetry, credentials, or personal data in tests or issue
reports.

Every pull request needs passing CI (lint and the test matrix) before merge.
Pull requests are rebase merged, so each commit lands on `main` as written,
with its own author. Keep every commit focused, passing, and written as a
Conventional Commit.

## Policy and guidance changes

A policy change must be made in `scripts/build_policy_pack.py`. It must include
a concrete trigger, bounded output, false-positive risk, and a measurable user
outcome. Regenerate the pack and commit both files:

```bash
python scripts/build_policy_pack.py
python scripts/build_policy_pack.py --check
```

A bundled guidance change must be made in
`scripts/build_guidance_pack.py`. Regenerate the pack and commit both files:

```bash
python scripts/build_guidance_pack.py
python scripts/build_guidance_pack.py --check
```

Guidance must remain advisory. It cannot request permissions, weaken project
rules, or make unsupported savings claims.

## Commit messages

Use [Conventional Commits](https://www.conventionalcommits.org/): a type
(`feat`, `fix`, `docs`, `chore`, `ci`, `test`, `refactor`, `style`), an
optional scope, and a short imperative summary, for example
`fix(hook): count each tool call once`. The commit-msg hook checks the format.
Explain user-visible behavior in the commit body when the reason is not clear
from the diff.
