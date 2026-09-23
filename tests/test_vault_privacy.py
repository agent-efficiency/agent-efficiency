"""No vault identifier may reach the runtime store, its journals, or a report.

The owner, repo, and branch canaries are selection inputs: they pick the
project note and never reach host output. Every other canary does reach the
host output in at least one case below. That is checked too, so a pass means
the value was handled and kept out, not that it was never read. The receipt
list is checked in full, so a path that silently did nothing fails the test.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from agent_efficiency.hook import run_hook
from agent_efficiency.report import build_report, format_report
from agent_efficiency.store import Store
from vault_fixtures import make_repo, make_vault_tree, note_text, register_trees

CANARIES = (
    "canaryid7731",
    "canarytitle7731",
    "canaryhook7731",
    "canaryowner7731",
    "canaryrepo7731",
    "canarybranch7731",
    "canaryvault7731",
    "canarybody7731",
    "canarytie7731",
)
CLAUDE_ENV = {"CLAUDE_PLUGIN_ROOT": "/plugin"}


class VaultPrivacyCanaryTests(unittest.TestCase):
    def test_no_vault_identifier_reaches_the_store_or_the_report(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            base = Path(temp).resolve()
            store = Store(base / "data")
            project = make_repo(
                base / "project",
                remotes={"origin": "git@git.example.com:canaryowner7731/canaryrepo7731.git"},
                branch="canarybranch7731",
            )
            (project / "src").mkdir()
            tie = base / "tie"
            tie.mkdir()
            core = make_vault_tree(
                base / "canaryvault7731-core",
                "core",
                [
                    note_text(
                        "canaryid7731-rule",
                        note_type="feedback",
                        classification="core",
                        hook="canaryhook7731 rule",
                    )
                ],
            )
            private = make_vault_tree(
                base / "canaryvault7731-private",
                "private",
                [
                    note_text(
                        "canaryid7731",
                        title="canarytitle7731",
                        hook="canaryhook7731",
                        repos=("canaryowner7731/canaryrepo7731",),
                        branches=("canarybranch7731",),
                        body="canarybody7731",
                    ),
                    note_text("canarytie7731-a", paths=(str(tie),)),
                    note_text("canarytie7731-b", paths=(str(tie),)),
                ],
            )
            register_trees(store.paths.root, core, private)

            def claude(session: str, cwd: Path, event: str, **extra: object):
                payload = {
                    "session_id": session,
                    "cwd": str(cwd),
                    "hook_event_name": event,
                    **extra,
                }
                return run_hook(payload, store=store, environ=CLAUDE_ENV)

            first = claude("s1", project / "src", "SessionStart", source="startup")
            claude(
                "s1",
                project / "src",
                "UserPromptSubmit",
                prompt="$agent-efficiency vault",
            )
            claude(
                "s1",
                project / "src",
                "UserPromptSubmit",
                prompt="Explain the parser.",
                prompt_id="t1",
            )
            claude("s1", project / "src", "SessionStart", source="resume")
            claude("s1", project / "src", "SessionStart", source="compact")
            tied = claude("s2", tie, "SessionStart", source="startup")
            run_hook(
                {
                    "conversation_id": "s3",
                    "generation_id": "g-1",
                    "cursor_version": "1.7.2",
                    "workspace_roots": [str(project)],
                    "hook_event_name": "postToolUse",
                    "tool_name": "Read",
                    "tool_input": {"file_path": str(project / "README")},
                    "tool_output": '{"success":true}',
                    "duration": 5,
                },
                store=store,
            )
            (private / "index.json").write_text("{", encoding="utf-8")
            claude("s4", project / "src", "SessionStart", source="startup")

            delivered = first["hookSpecificOutput"]["additionalContext"]
            for canary in (
                "canaryid7731",
                "canarytitle7731",
                "canaryhook7731",
                "canaryvault7731",
                "canarybody7731",
            ):
                self.assertIn(canary, delivered)
            self.assertIn(
                "canarytie7731-a", tied["hookSpecificOutput"]["additionalContext"]
            )
            with closing(sqlite3.connect(store.paths.database)) as conn:
                receipts = conn.execute(
                    "SELECT session_id, cause, disposition, reason_code "
                    "FROM vault_receipts ORDER BY id"
                ).fetchall()
            self.assertEqual(
                receipts,
                [
                    ("s1", "new", "delivered", None),
                    ("s1", "request", "delivered", None),
                    ("s1", "resume", "skipped", "unchanged"),
                    ("s1", "compact", "delivered", None),
                    ("s2", "new", "delivered", "ambiguous"),
                    ("s3", "deferred", "deferred", None),
                    ("s4", "new", "degraded", "parse_error"),
                ],
            )

            report = build_report(store, 30)
            surfaces = {
                "report json": json.dumps(report),
                "report text": format_report(report),
            }
            for path in sorted(store.paths.root.rglob("*")):
                # vault.json is the registered tree list. It holds tree paths by
                # design and is the one file this rule does not cover.
                if path.is_file() and path.name != "vault.json":
                    surfaces[str(path.relative_to(store.paths.root))] = (
                        path.read_bytes().decode("utf-8", errors="replace")
                    )
            with closing(sqlite3.connect(store.paths.database)) as conn:
                surfaces["sql dump"] = "\n".join(conn.iterdump())

            for name, content in surfaces.items():
                for canary in CANARIES:
                    with self.subTest(surface=name, canary=canary):
                        self.assertNotIn(canary, content)


if __name__ == "__main__":
    unittest.main()
