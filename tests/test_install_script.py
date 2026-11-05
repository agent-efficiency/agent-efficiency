"""install.sh, run against stand-ins for claude, pipx, uvx, and the command.

The stand-ins record every call, so these tests check what the script asks
each tool to do without a network, a real host, or a real install. The
release checks run the script for real on a throwaway home folder.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "install.sh"
SYSTEM_TOOLS = ("bash", "cat", "cp", "mkdir", "chmod", "date", "grep", "git", "env")

CLAUDE = r"""#!/usr/bin/env python3
import json, os, sys
log = os.environ["STUB_LOG"]
state_path = os.environ["STUB_STATE"]
with open(log, "a") as handle:
    handle.write("claude " + " ".join(sys.argv[1:]) + "\n")
state = json.load(open(state_path)) if os.path.exists(state_path) else {"marketplaces": [], "plugins": []}
args = sys.argv[1:]
if args[:3] == ["plugin", "marketplace", "list"]:
    print(json.dumps([{"name": name} for name in state["marketplaces"]], indent=2))
elif args[:2] == ["plugin", "list"]:
    print(json.dumps([{"id": name} for name in state["plugins"]], indent=2))
elif args[:3] == ["plugin", "marketplace", "add"]:
    state["marketplaces"].append("agent-efficiency")
elif args[:3] == ["plugin", "marketplace", "remove"]:
    state["marketplaces"].remove(args[3])
    state["plugins"] = []
elif args[:2] == ["plugin", "install"]:
    state["plugins"].append(args[2])
elif args[:2] == ["plugin", "uninstall"]:
    state["plugins"].remove(args[2])
json.dump(state, open(state_path, "w"))
"""

COMMAND = r"""#!/usr/bin/env bash
echo "agent-efficiency data=${AGENT_EFFICIENCY_DATA:-} $*" >> "$STUB_LOG"
"""

PIPX = r"""#!/usr/bin/env bash
echo "PIPX_NAME $*" >> "$STUB_LOG"
mkdir -p "$HOME/.local/bin"
cp "$STUB_DIR/agent-efficiency.stub" "$HOME/.local/bin/agent-efficiency"
chmod +x "$HOME/.local/bin/agent-efficiency"
"""

OLD_PYTHON = r"""#!/usr/bin/env bash
case "$*" in
  *">= (3, 11)"*) exit 1 ;;
  *) echo "3.10.12" ;;
esac
"""


def write_tool(folder: Path, name: str, text: str) -> None:
    path = folder / name
    path.write_text(text, encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


class InstallScriptTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.log = self.root / "calls.log"
        self.state = self.root / "claude-state.json"
        for tool in SYSTEM_TOOLS:
            found = shutil.which(tool)
            if found:
                (self.bin / tool).symlink_to(found)
        (self.bin / "python3").symlink_to(sys.executable)
        write_tool(self.bin, "claude", CLAUDE.replace("python3", sys.executable, 1))
        write_tool(self.bin, "pipx", PIPX.replace("PIPX_NAME", "pipx"))
        write_tool(self.bin, "agent-efficiency.stub", COMMAND)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def run_script(self, *args: str) -> subprocess.CompletedProcess[str]:
        environment = {
            "HOME": str(self.home),
            "PATH": str(self.bin),
            "LANG": "C.UTF-8",
            "STUB_LOG": str(self.log),
            "STUB_STATE": str(self.state),
            "STUB_DIR": str(self.bin),
        }
        return subprocess.run(
            [str(self.bin / "bash"), str(SCRIPT), *args],
            env=environment,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def calls(self) -> list[str]:
        return (
            self.log.read_text(encoding="utf-8").splitlines()
            if self.log.exists()
            else []
        )

    def settings(self) -> dict:
        return json.loads((self.home / ".claude" / "settings.json").read_text())

    def make_vault(self, name: str, classification: str) -> Path:
        tree = self.root / name
        tree.mkdir()
        (tree / ".vault.json").write_text(
            json.dumps({"schema": 1, "classification": classification}),
            encoding="utf-8",
        )
        return tree

    def test_fresh_install(self) -> None:
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        self.assertIn(
            "claude plugin marketplace add agent-efficiency/agent-efficiency#v0.2.2",
            calls,
        )
        self.assertIn(
            "claude plugin install agent-efficiency@agent-efficiency --scope user",
            calls,
        )
        self.assertIn(
            "pipx install --force git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2",
            calls,
        )
        self.assertEqual(calls[-1], f"agent-efficiency data= doctor --cwd {self.home}")
        self.assertEqual(self.settings(), {})
        backups = list(
            (self.home / ".claude").glob("settings.json.agent-efficiency-backup-*")
        )
        self.assertEqual(len(backups), 1)
        self.assertIn("/reload-plugins", result.stdout.splitlines()[-1])

    def test_settings_update_keeps_private_permissions(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}\n", encoding="utf-8")
        settings.chmod(0o600)
        previous_umask = os.umask(0o022)
        try:
            result = self.run_script("--data-dir", str(self.root / "data"))
        finally:
            os.umask(previous_umask)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stat.S_IMODE(settings.stat().st_mode), 0o600)

    def test_settings_update_does_not_follow_a_temporary_link(self) -> None:
        settings = self.home / ".claude" / "settings.json"
        settings.parent.mkdir()
        settings.write_text("{}\n", encoding="utf-8")
        unrelated = self.root / "unrelated.json"
        unrelated.write_text("retained\n", encoding="utf-8")
        settings.with_suffix(".json.tmp").symlink_to(unrelated)
        result = self.run_script("--data-dir", str(self.root / "data"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(unrelated.read_text(encoding="utf-8"), "retained\n")
        self.assertFalse(settings.is_symlink())

    def test_second_run_replaces_the_marketplace_and_keeps_data(self) -> None:
        self.assertEqual(self.run_script().returncode, 0)
        self.log.unlink()
        result = self.run_script("--ref", "v9.9.9")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        uninstall = calls.index(
            "claude plugin uninstall agent-efficiency@agent-efficiency --scope user --keep-data"
        )
        remove = calls.index("claude plugin marketplace remove agent-efficiency")
        add = calls.index(
            "claude plugin marketplace add agent-efficiency/agent-efficiency#v9.9.9"
        )
        self.assertLess(uninstall, remove)
        self.assertLess(remove, add)
        self.assertIn(
            "pipx install --force git+https://github.com/agent-efficiency/agent-efficiency@v9.9.9",
            calls,
        )

    def test_data_dir_and_vaults(self) -> None:
        core = self.make_vault("vault-core", "core")
        work = self.make_vault("vault-work", "work")
        data = self.root / "data"
        result = self.run_script(
            "--data-dir", str(data), "--vault", str(core), "--vault", str(work)
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.settings()["env"]["AGENT_EFFICIENCY_DATA"], str(data))
        self.assertEqual(stat.S_IMODE(data.stat().st_mode), 0o700)
        calls = self.calls()
        self.assertIn(
            f"agent-efficiency data= vault init {core} --classification core", calls
        )
        self.assertIn(
            f"agent-efficiency data= --data-dir {data} vault register {work}", calls
        )
        self.assertEqual(
            calls[-1], f"agent-efficiency data={data} doctor --cwd {self.home}"
        )

    def test_stale_directory_marketplace_is_removed(self) -> None:
        folder = self.home / ".claude"
        folder.mkdir()
        (folder / "settings.json").write_text(
            json.dumps(
                {
                    "model": "example",
                    "extraKnownMarketplaces": {
                        "agent-efficiency": {
                            "source": {
                                "source": "directory",
                                "path": str(self.root / "gone"),
                            }
                        },
                        "other": {"source": {"source": "directory", "path": "/gone"}},
                    },
                }
            ),
            encoding="utf-8",
        )
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            self.settings(),
            {
                "model": "example",
                "extraKnownMarketplaces": {
                    "other": {"source": {"source": "directory", "path": "/gone"}}
                },
            },
        )
        self.assertIn("removed the agent-efficiency marketplace entry", result.stdout)

    def test_uvx_is_used_when_pipx_is_missing(self) -> None:
        (self.bin / "pipx").unlink()
        write_tool(self.bin, "uvx", PIPX.replace("PIPX_NAME", "uvx"))
        result = self.run_script()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(
            "uvx --from pipx pipx install --force "
            "git+https://github.com/agent-efficiency/agent-efficiency@v0.2.2",
            self.calls(),
        )

    def test_stops_without_pipx_or_uvx(self) -> None:
        (self.bin / "pipx").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("neither pipx nor uvx is on PATH", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_stops_without_claude(self) -> None:
        (self.bin / "claude").unlink()
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("the claude command is not on PATH", result.stderr)

    def test_stops_on_old_python(self) -> None:
        (self.bin / "python3").unlink()
        write_tool(self.bin, "python3", OLD_PYTHON)
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("python3 is Python 3.10.12", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_stops_when_a_vault_is_not_a_tree(self) -> None:
        folder = self.root / "notes"
        folder.mkdir()
        result = self.run_script("--vault", str(folder))
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not a vault tree", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_no_command_needs_the_command_on_path(self) -> None:
        result = self.run_script("--no-command")
        self.assertEqual(result.returncode, 1)
        self.assertIn("--no-command needs agent-efficiency on PATH", result.stderr)

    def test_invalid_settings_are_left_alone(self) -> None:
        folder = self.home / ".claude"
        folder.mkdir()
        (folder / "settings.json").write_text("{not json", encoding="utf-8")
        result = self.run_script()
        self.assertEqual(result.returncode, 1)
        self.assertIn("is not valid JSON", result.stderr)
        self.assertEqual((folder / "settings.json").read_text(), "{not json")
        self.assertNotIn("claude plugin install", "\n".join(self.calls()))


if __name__ == "__main__":
    unittest.main()
