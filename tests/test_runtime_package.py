from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from agent_efficiency.paths import PLUGIN_ROOT


class RuntimePackageTests(unittest.TestCase):
    def test_installed_tree_excludes_repository_maintenance_tools(self) -> None:
        forbidden = (
            "components",
            "sources.json",
            "src/agent_efficiency/knowledge.py",
            "src/agent_efficiency/capability_refinery.py",
            "src/agent_efficiency/capability_release.py",
            "src/agent_efficiency/validation.py",
            "schemas/knowledge-card.schema.json",
            "schemas/sources.schema.json",
        )
        self.assertTrue(all(not (PLUGIN_ROOT / path).exists() for path in forbidden))
        self.assertTrue(
            (
                PLUGIN_ROOT / "src/agent_efficiency/bundled/capabilities/base-pack.json"
            ).is_file()
        )

    def test_cache_free_installed_tree_stays_below_600_kib(self) -> None:
        files = [
            path
            for path in PLUGIN_ROOT.rglob("*")
            if path.is_file()
            and "__pycache__" not in path.parts
            and path.suffix not in {".pyc", ".pyo"}
        ]
        self.assertLessEqual(sum(path.stat().st_size for path in files), 600 * 1024)

    def test_runtime_cli_imports_without_the_maintainer_tree(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            target = Path(temp) / "agent-efficiency"
            shutil.copytree(
                PLUGIN_ROOT,
                target,
                ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.pyo"),
            )
            environment = {
                "PATH": os.environ.get("PATH", ""),
                "PYTHONPATH": str(target / "src"),
            }
            result = subprocess.run(
                [sys.executable, "-m", "agent_efficiency", "--help"],
                cwd=target,
                env=environment,
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("ingest-bundled", result.stdout)
        self.assertNotIn("validate", result.stdout)


if __name__ == "__main__":
    unittest.main()
