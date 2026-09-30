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

```bash
git clone https://github.com/agent-efficiency/agent-efficiency.git
cd agent-efficiency
python3 -m venv .venv
. .venv/bin/activate
python -m pip install -e .
```

Run the full local checks:

```bash
python -m unittest discover -s tests -v
python scripts/build_policy_pack.py --check
python scripts/build_guidance_pack.py --check
python scripts/validate_distribution.py
python scripts/check_wheel_install.py
python -m agent_efficiency doctor --json
```

## Pull requests

Keep pull requests focused. Include:

- the problem and behavior change;
- affected hosts;
- privacy and storage impact;
- tests and fixture evidence;
- known limits;
- documentation changes.

Do not include prompts, private repositories, source code from another
project, production telemetry, credentials, or personal data in tests or issue
reports.

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

Use a short imperative summary. Explain user-visible behavior in the commit
body when the reason is not clear from the diff.
