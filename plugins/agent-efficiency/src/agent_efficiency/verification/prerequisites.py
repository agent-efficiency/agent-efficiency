"""Read-only prerequisite readiness, never execution or completion approval."""

from __future__ import annotations

from collections.abc import Iterable

from agent_efficiency.store import Store, project_identity
from agent_efficiency.verification.config import CheckDefinition, ProjectConfig
from agent_efficiency.verification.receipts import receipt_state
from agent_efficiency.verification.environment import check_identity
from agent_efficiency.verification.state import workspace_state


def prerequisite_plan(
    config: ProjectConfig, checks: Iterable[CheckDefinition], store: Store
) -> dict:
    targets = list(dict.fromkeys(check.check_id for check in checks))
    by_id = {check.check_id: check for check in config.checks}
    if any(target not in by_id for target in targets):
        raise ValueError("unknown prerequisite plan target")
    order: list[str] = []
    seen: set[str] = set()
    prerequisites: set[str] = set()
    # Config loading validates cycles. Use an explicit stack for long chains.
    for target in targets:
        stack = [(target, False)]
        while stack:
            key, expanded = stack.pop()
            if key in seen:
                continue
            if expanded:
                seen.add(key)
                order.append(key)
                continue
            stack.append((key, True))
            dependencies = by_id[key].depends_on
            prerequisites.update(dependencies)
            stack.extend((dependency, False) for dependency in reversed(dependencies))
    state = workspace_state(config.root, config.digest)
    project_key, _ = project_identity(str(config.root))
    latest = {}
    for receipt in store.verification_receipts(project_key):
        latest.setdefault(receipt["check_id"], receipt)
    states = {
        key: receipt_state(
            latest.get(key),
            check_digest=check_identity(by_id[key], store),
            workspace_digest=state.digest,
            max_age_seconds=by_id[key].max_age_seconds,
        )
        for key in order
        if key in prerequisites
    }
    return {
        "targets": targets,
        "order": order,
        "prerequisites": states,
        "workspace": state.result,
        "ready": state.result == "ready"
        and all(value == "current" for value in states.values()),
        "advisory_only": True,
    }
