from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest import mock

from agent_efficiency.vault.render import (
    ALLOWANCE,
    CUT_NOTICE,
    HEAD_CAP,
    HEAD_UNREADABLE,
    HEADER,
    UNMAPPED,
    RenderError,
    read_body,
    render,
)
from agent_efficiency.vault.root import VaultTree
from agent_efficiency.vault.select import Selection
from vault_fixtures import note_text

TREES = {
    name: VaultTree(
        root=Path(f"/vault/{name}"),
        classification=name,
        preferred_remote="origin",
        remote_url="",
    )
    for name in ("core", "work", "private")
}
FOLDERS = {"project": "projects", "feedback": "feedback", "reference": "reference"}


def entry(
    note_id: str,
    note_type: str = "feedback",
    *,
    status: str = "active",
    hook: str | None = None,
) -> dict:
    return {
        "id": note_id,
        "title": f"{note_id} title",
        "type": note_type,
        "status": status,
        "hook": hook or f"{note_id} hook",
        "path": f"{FOLDERS[note_type]}/{note_id}.md",
    }


def matched(classification: str, project: dict) -> Selection:
    return Selection("matched", classification, project, ("core", classification))


def unmapped() -> Selection:
    return Selection("unmapped", None, None, ("core",))


def reader(bodies: dict):
    def read(tree: VaultTree, note: dict) -> str:
        value = bodies[note["id"]]
        if isinstance(value, Exception):
            raise value
        return value

    return read


class RenderTests(unittest.TestCase):
    def test_head_then_core_then_tree_notes_then_other_projects(self) -> None:
        head = entry("catalog", "project")
        idx = {
            "core": {"notes": [entry("writing"), entry("links", "reference")]},
            "private": {"notes": [entry("style"), head, entry("calendar", "project")]},
        }
        out = render(
            matched("private", head),
            TREES,
            idx,
            read=reader({"catalog": "Next: API slice."}),
        )
        markers = (
            "Next: API slice.",
            "- feedback/writing: writing hook",
            "- reference/links: links hook",
            "- feedback/style: style hook",
            "- projects/calendar: calendar hook",
        )
        positions = [out.text.index(marker) for marker in markers]
        self.assertEqual(positions, sorted(positions))
        self.assertTrue(out.text.startswith(HEADER))
        self.assertNotIn("projects/catalog:", out.text)
        self.assertEqual(out.notes_selected, 5)
        self.assertEqual(out.head_chars, len("Next: API slice."))
        self.assertEqual(
            (out.omitted, out.chars_omitted, out.truncated, out.head_error),
            ((), 0, False, False),
        )

    def test_dormant_notes_are_left_out(self) -> None:
        idx = {"core": {"notes": [entry("old", status="dormant")]}}
        out = render(unmapped(), TREES, idx)
        self.assertNotIn("feedback/old", out.text)
        self.assertIn(UNMAPPED, out.text)

    def test_allowance_is_never_exceeded_and_omissions_are_counted(self) -> None:
        many = [entry(f"rule-{n:03d}", hook="x" * 90) for n in range(200)]
        out = render(unmapped(), TREES, {"core": {"notes": many}}, allowance=3000)
        self.assertLessEqual(len(out.text), 3000)
        self.assertIn("Omitted for space:", out.text)
        self.assertEqual(out.notes_selected + dict(out.omitted)["notes"], 200)
        self.assertGreater(out.chars_omitted, 0)

    def test_everything_that_fits_renders_without_an_omission_line(self) -> None:
        out = render(unmapped(), TREES, {"core": {"notes": [entry("a"), entry("b")]}})
        self.assertNotIn("Omitted", out.text)
        self.assertEqual(out.notes_selected, 2)

    def test_oversized_head_is_cut_at_a_line_and_reported(self) -> None:
        head = entry("big", "project")
        body = "\n".join(f"line {n} " + "y" * 60 for n in range(100))
        out = render(
            matched("work", head),
            TREES,
            {"core": {"notes": []}, "work": {"notes": [head]}},
            read=reader({"big": body}),
        )
        self.assertEqual((out.truncated, out.over_cap), (True, True))
        self.assertLessEqual(out.head_chars, HEAD_CAP)
        self.assertIn(f"over its {HEAD_CAP} character cap", out.text)

    def test_matched_head_with_an_empty_body_is_selected(self) -> None:
        head = entry("fresh", "project")
        out = render(
            matched("work", head),
            TREES,
            {"core": {"notes": []}, "work": {"notes": [head]}},
            read=reader({"fresh": ""}),
        )
        self.assertEqual((out.notes_selected, out.head_chars), (1, 0))
        self.assertIn("## Project: fresh title", out.text)

    def test_head_whose_file_is_over_cap_is_flagged_but_not_cut(self) -> None:
        head = {**entry("full", "project"), "within_cap": False}
        body = "x" * (HEAD_CAP - 10)
        out = render(
            matched("work", head),
            TREES,
            {"core": {"notes": []}, "work": {"notes": [head]}},
            read=reader({"full": body}),
        )
        self.assertEqual((out.over_cap, out.truncated), (True, False))
        self.assertEqual(out.head_chars, len(body))
        self.assertIn(body, out.text)
        self.assertIn(f"over its {HEAD_CAP} character cap", out.text)

    def test_unreadable_head_is_reported_and_core_still_renders(self) -> None:
        head = entry("gone", "project")
        out = render(
            matched("work", head),
            TREES,
            {"core": {"notes": [entry("rule")]}, "work": {"notes": [head]}},
            read=reader({"gone": RenderError("missing")}),
        )
        self.assertTrue(out.head_error)
        self.assertIn(HEAD_UNREADABLE, out.text)
        self.assertIn("feedback/rule", out.text)
        self.assertEqual(out.head_chars, 0)

    def test_ambiguous_names_the_tied_ids(self) -> None:
        selection = Selection("ambiguous", None, None, ("core",), ("a", "b"))
        out = render(selection, TREES, {"core": {"notes": []}})
        self.assertIn("(a, b)", out.text)

    def test_long_title_is_cut_and_the_allowance_holds(self) -> None:
        head = {**entry("long", "project"), "title": "t" * 6500}
        out = render(
            matched("work", head),
            TREES,
            {"core": {"notes": []}, "work": {"notes": [head]}},
            read=reader({"long": "b" * 2950}),
        )
        self.assertLessEqual(len(out.text), ALLOWANCE)
        self.assertIn("## Project: " + "t" * 100 + "\n", out.text)

    def test_long_ambiguous_tie_is_cut_to_the_allowance(self) -> None:
        tied = tuple(f"project-{n:03d}" for n in range(200))
        selection = Selection("ambiguous", None, None, ("core",), tied)
        out = render(selection, TREES, {"core": {"notes": []}}, allowance=1000)
        self.assertLessEqual(len(out.text), 1000)
        self.assertTrue(out.text.endswith(CUT_NOTICE))
        self.assertTrue(out.truncated)

    def test_rendering_is_deterministic(self) -> None:
        idx = {"core": {"notes": [entry(f"r{n}") for n in range(30)]}}
        self.assertEqual(
            render(unmapped(), TREES, idx).text, render(unmapped(), TREES, idx).text
        )


class ReadBodyTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name).resolve() / "tree"
        (root / "projects").mkdir(parents=True)
        self.tree = VaultTree(
            root=root, classification="work", preferred_remote="origin", remote_url=""
        )
        (root / "projects" / "app.md").write_text(
            note_text("app", classification="work", body="Body text."),
            encoding="utf-8",
        )

    def test_returns_the_body_without_frontmatter(self) -> None:
        self.assertEqual(
            read_body(self.tree, {"path": "projects/app.md"}), "Body text."
        )

    def test_refuses_a_path_outside_the_tree(self) -> None:
        with self.assertRaises(RenderError):
            read_body(self.tree, {"path": "../outside.md"})

    def test_missing_file_is_a_render_error(self) -> None:
        with self.assertRaises(RenderError):
            read_body(self.tree, {"path": "projects/none.md"})

    def test_symlink_loop_head_is_a_head_error(self) -> None:
        loop = self.tree.root / "projects" / "loop.md"
        loop.symlink_to(loop)
        real_resolve = Path.resolve

        def resolve(path: Path, strict: bool = False) -> Path:
            # Python 3.13 and later resolve a loop without raising.
            if path.name == "loop.md":
                raise RuntimeError(f"Symlink loop from {path}")
            return real_resolve(path, strict)

        head = {**entry("loop", "project"), "path": "projects/loop.md"}
        with mock.patch.object(Path, "resolve", resolve):
            out = render(
                matched("work", head),
                {**TREES, "work": self.tree},
                {"core": {"notes": []}, "work": {"notes": [head]}},
            )
        self.assertTrue(out.head_error)
        self.assertIn(HEAD_UNREADABLE, out.text)


if __name__ == "__main__":
    unittest.main()
