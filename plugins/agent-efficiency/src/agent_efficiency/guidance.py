"""Present local search suggestions and apply explicit host prompt choices.

Candidate text stays in the project file and host output. Hooks do no model
work. A candidate is data until a user chooses its exact version and scope.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

from agent_efficiency import guidance_state as ledger
from agent_efficiency.adapters import adapter_for
from agent_efficiency.contracts.effects import CanonicalEffect
from agent_efficiency.store import Store, project_identity

FILE_NAME = "agent-efficiency-guidance.json"
MAX_FILE_BYTES = 32_768
PREFIX = r"\s*(?:\$|/)?agent-efficiency(?::agent-efficiency)?\s+guidance"
CONTROL_PREFIX = re.compile(PREFIX + r"(?:\s|$)", re.IGNORECASE)
CHOICE = re.compile(
    PREFIX + r"\s+([0-9a-f]{16})\s+(once|similar|later|never)\s*", re.IGNORECASE
)
RESET = re.compile(PREFIX + r"\s+reset(?:\s+([0-9a-f]{16}))?\s*", re.IGNORECASE)
STATUS = re.compile(PREFIX + r"\s+status\s*", re.IGNORECASE)
KEYS = {"kind", "target", "terms", "title", "reason", "source", "expires"}


@dataclass(frozen=True)
class Candidate:
    target: str
    terms: tuple[str, ...]
    title: str
    reason: str
    source: str
    action: str
    version: str

    def matches(self, prompt: str) -> bool:
        words = set(re.findall(r"[a-z0-9]+", prompt.casefold()))
        return set(self.terms) <= words

    def tip(self) -> str:
        return (
            "Agent Efficiency adopted guidance, current request only: "
            f"start searches in {self.target}. Broaden when needed. "
            "Keep the full task scope and all required checks. "
            "Continue the request that prompted this suggestion. "
            "This choice is not evidence of savings."
        )

    def notice(self, token: str) -> str:
        command = f"agent-efficiency guidance {token}"
        return (
            f"Agent Efficiency suggestion\nNot active. {self.title}\n"
            f"Why: this request matches {', '.join(self.terms)}. {self.reason}\n"
            f"Source stated by the proposal: {self.source}\n"
            f"Proposed change: start searches in {self.target}; broaden when needed. "
            "Keep all required checks.\n"
            "Expected benefit: fewer irrelevant search results. Savings have not been measured.\n"
            "Scope: this request, or future requests matching all these terms in this "
            "working directory. No choice means no change.\n"
            f"Use once: {command} once\n"
            f"Use for similar tasks: {command} similar\n"
            f"Not now (wait at least 24 hours): {command} later\n"
            f"Don't suggest again (this search location in this working directory): {command} never"
        )


def load_candidates(cwd: Path, *, today: date | None = None) -> tuple[Candidate, ...]:
    """Read one bounded, explicit project file. Do not search parent directories."""
    path = cwd / FILE_NAME
    if not path.exists():
        return ()
    if path.is_symlink() or not path.is_file():
        raise ValueError("guidance must be a regular project file")
    with path.open("rb") as stream:
        raw = stream.read(MAX_FILE_BYTES + 1)
    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("guidance file exceeds 32768 bytes")
    data = json.loads(raw)
    return validate_candidates(data, cwd, today=today)


def validate_candidates(
    data: Any, cwd: Path, *, today: date | None = None
) -> tuple[Candidate, ...]:
    """Validate an in-memory proposal document without writing it."""
    if (
        not isinstance(data, dict)
        or set(data) != {"schema", "suggestions"}
        or type(data["schema"]) is not int
        or data["schema"] != 1
        or not isinstance(data["suggestions"], list)
        or len(data["suggestions"]) > 20
    ):
        raise ValueError("expected guidance schema 1 with at most 20 suggestions")
    result = []
    seen = set()
    for row in data["suggestions"]:
        if not isinstance(row, dict) or set(row) != KEYS:
            raise ValueError("invalid suggestion fields")
        for key, cap in (
            ("title", 80),
            ("reason", 240),
            ("source", 160),
            ("target", 120),
        ):
            value = row[key]
            if (
                not isinstance(value, str)
                or not value.strip()
                or len(value) > cap
                or any(ord(char) < 32 or ord(char) == 127 for char in value)
            ):
                raise ValueError(f"invalid suggestion {key}")
        if row["kind"] != "search-start":
            raise ValueError("only search-start suggestions are supported")
        target = row["target"]
        if not re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_./-]*", target) or any(
            part in {"", ".", ".."} for part in target.split("/")
        ):
            raise ValueError("target must be a relative component directory")
        resolved = (cwd / target).resolve()
        if not resolved.is_dir() or not resolved.is_relative_to(cwd.resolve()):
            raise ValueError("target must exist within this working directory")
        terms = row["terms"]
        if (
            not isinstance(terms, list)
            or not 1 <= len(terms) <= 8
            or any(
                not isinstance(term, str)
                or not re.fullmatch(r"[a-z][a-z0-9]{2,31}", term)
                for term in terms
            )
            or len(set(terms)) != len(terms)
        ):
            raise ValueError("terms must be one to eight distinct lowercase words")
        if not isinstance(row["expires"], str):
            raise ValueError("expires must be a date")
        expires = date.fromisoformat(row["expires"])
        # The action key excludes presentation and match terms. Renaming a
        # rejected proposal or changing its terms cannot evade the rejection.
        canonical_target = resolved.relative_to(cwd.resolve()).as_posix()
        action = ledger.digest("search-start:" + canonical_target)
        if action in seen:
            raise ValueError("duplicate search location")
        seen.add(action)
        canonical = {**row, "terms": sorted(terms), "target": canonical_target}
        version = ledger.digest(json.dumps(canonical, sort_keys=True))
        if expires <= (today or date.today()):
            continue
        result.append(
            Candidate(
                target,
                tuple(sorted(terms)),
                row["title"],
                row["reason"],
                row["source"],
                action,
                version,
            )
        )
    return tuple(result)


def _render(host: str, text: str, *, effect: str = "notice") -> dict[str, Any]:
    return (
        adapter_for(host).render(
            CanonicalEffect(
                effect=effect,
                policy_id="guidance.choice",
                policy_revision="1",
                observed_fact="explicit_choice"
                if effect == "notify"
                else "scope_match",
                agent_message="",
                user_message=text,
            ),
            native_event="UserPromptSubmit",
        )
        or {}
    )


def _context(text: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": text,
        }
    }


def is_control(prompt: str) -> bool:
    return CONTROL_PREFIX.match(prompt) is not None


def format_status(store: Store, cwd: Path, mode: str, session: str) -> str:
    candidates = load_candidates(cwd)
    project = project_identity(str(cwd))[0]
    lines = [
        "Current guidance proposals in this working directory.",
        f"Session mode: {mode}.",
    ]
    if ledger.in_experiment(store, session):
        lines.append("Guidance is disabled during this enrolled experiment.")
    with store.connect() as conn:
        exists = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE name = 'guidance_state_v1'"
        ).fetchone()
        for item in candidates:
            state = ledger.state(conn, project, item.action) if exists else {}
            choice = state.get("choice")
            if choice == "never":
                label = "Rejected"
            elif choice == "later":
                label = "Deferred; not adopted"
            elif choice == "similar":
                label = (
                    "Adopted"
                    if state.get("version") == item.version
                    else "Changed; needs a new choice"
                )
            else:
                label = "Not adopted"
            lines.append(
                f"{label}: {item.target}. Matching words: {', '.join(item.terms)}."
            )
            if state:
                lines.append(
                    f"Clear this choice: agent-efficiency guidance reset {item.action}"
                )
    if not candidates:
        lines.append(
            "No current proposals. Expired and removed proposals are not listed."
        )
    lines.append(
        "No guidance was applied or choices changed. To clear saved choices, send agent-efficiency guidance reset."
    )
    return "\n".join(lines)


def handle_control(
    store: Store, session: str, host: str, cwd: Path, prompt: str, mode: str
):
    """Only the dispatcher calls this, on a native user prompt event."""

    def reply(text):
        return _render(host, "Agent Efficiency: " + text, effect="notify")

    if host not in {"claude", "codex"}:
        return reply(
            "Guidance choices are not supported by this host yet. No guidance was applied."
        )
    project = project_identity(str(cwd))[0]
    request = ledger.digest(store.latest_turn_key(session) or "")
    if STATUS.fullmatch(prompt):
        return reply(format_status(store, cwd, mode, session))
    reset_match = RESET.fullmatch(prompt)
    if reset_match:
        action = reset_match[1].lower() if reset_match[1] else None
        with ledger.transaction(store) as conn:
            changed = ledger.reset(conn, project, session, request, action)
        if not changed:
            return reply(
                "No saved choice matches that ID in this working directory. No choices changed."
            )
        if action:
            return reply(
                "One guidance choice was cleared. Other choices remain saved. "
                "Earlier context may still be in this conversation. Start a new session to clear it."
            )
        return reply(
            "Guidance choices for this working directory were reset. No future adoption remains. "
            "Earlier context may still be in this conversation. Start a new session to clear it."
        )
    choice = CHOICE.fullmatch(prompt)
    if not choice:
        return reply(
            "Use an exact choice command from the suggestion, agent-efficiency guidance status, or agent-efficiency guidance reset."
        )
    if mode not in {"advise", "guard"}:
        return reply(
            "Guidance choices require advise or guard mode. No choice was saved."
        )
    if ledger.in_experiment(store, session):
        return reply(
            "Guidance choices are unavailable during an enrolled experiment. No choice was saved."
        )
    candidates = load_candidates(cwd)
    with ledger.transaction(store) as conn:
        request = ledger.current_request(conn, session)
        offered = ledger.pending(conn, choice[1].lower(), project, session, request)
        candidate = next(
            (
                item
                for item in candidates
                if offered and item.version == offered["version"]
            ),
            None,
        )
        if not offered or not candidate:
            return reply(
                "This suggestion is stale, changed, already handled, or belongs to another request. No choice was saved."
            )
        selected = choice[2].lower()
        request_open = ledger.request_is_open(conn, session)
        if selected == "once" and not request_open:
            return reply(
                "The request has ended. No guidance was applied or choice saved. "
                "You can still choose use for similar tasks for future requests."
            )
        ledger.choose(conn, offered, selected)
        if selected in {"later", "never"}:
            return reply(
                "No guidance was applied. "
                + (
                    "This search location will not be suggested again in this working directory."
                    if selected == "never"
                    else "The suggestion is deferred for at least 24 hours."
                )
            )
        if not request_open:
            return reply(
                "Guidance adopted for matching future requests. "
                "The ended request was not restarted."
            )
        text = candidate.tip()
        if not ledger.context_fits(conn, session, len(text)):
            ledger.record(
                conn,
                project,
                session,
                request,
                candidate.action,
                candidate.version,
                "budget_exceeded",
            )
            return reply(
                "Choice saved, but guidance did not fit the context allowance and was not applied. "
                + (
                    "It remains adopted for matching future requests."
                    if selected == "similar"
                    else "Use once has ended."
                )
            )
        ledger.record(
            conn,
            project,
            session,
            request,
            candidate.action,
            candidate.version,
            "context_prepared",
            len(text),
        )
        output = _context(text)
        output.update(
            _render(
                host,
                "Agent Efficiency: guidance added for this request. "
                + (
                    "Also adopted for matching future requests in this working directory."
                    if selected == "similar"
                    else "It will not be reused automatically."
                ),
            )
        )
        return output


def on_prompt(
    store: Store,
    session: str,
    host: str,
    cwd: Path,
    prompt: str,
    mode: str,
    *,
    request_key: str,
):
    if mode not in {"advise", "guard"} or host not in {"claude", "codex"}:
        return None
    candidates = [item for item in load_candidates(cwd) if item.matches(prompt)]
    if not candidates:
        return None
    if ledger.in_experiment(store, session):
        return None
    project = project_identity(str(cwd))[0]
    request = ledger.digest(request_key)
    with ledger.transaction(store) as conn:
        if ledger.current_request(conn, session) != request:
            return None
        if not ledger.request_is_open(conn, session):
            return None
        if ledger.request_handled(conn, session, request):
            return None
        # An adopted tip takes priority over a new suggestion. At most one
        # learned tip or new proposal is returned for a request.
        for item in candidates:
            state = ledger.state(conn, project, item.action)
            if (
                state.get("choice") == "similar"
                and state.get("version") == item.version
            ):
                text = item.tip()
                if not ledger.context_fits(conn, session, len(text)):
                    ledger.record(
                        conn,
                        project,
                        session,
                        request,
                        item.action,
                        item.version,
                        "budget_exceeded",
                    )
                    return None
                ledger.record(
                    conn,
                    project,
                    session,
                    request,
                    item.action,
                    item.version,
                    "context_prepared",
                    len(text),
                )
                return _context(text)
        for item in candidates:
            if ledger.can_offer(conn, project, item.action):
                token = ledger.offer(
                    conn, project, session, request, item.action, item.version
                )
                notice = item.notice(token)
                ledger.record(
                    conn,
                    project,
                    session,
                    request,
                    item.action,
                    item.version,
                    "notice_prepared",
                    len(notice),
                )
                return _render(host, notice)
    return None


def merge_output(existing, added):
    """Preserve operational guidance when adding the optional suggestion."""
    if not added:
        return existing
    output = dict(existing or {})
    for key, value in added.items():
        if key == "hookSpecificOutput":
            context = dict(output.get(key) or {})
            prior = context.get("additionalContext", "")
            context.update(value)
            context["additionalContext"] = "\n\n".join(
                filter(None, (prior, value.get("additionalContext", "")))
            )
            output[key] = context
        elif key == "systemMessage":
            output[key] = "\n\n".join(filter(None, (output.get(key), value)))
        else:
            output[key] = value
    return output
