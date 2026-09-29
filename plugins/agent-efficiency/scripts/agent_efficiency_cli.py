#!/usr/bin/env python3
"""Self-contained CLI entrypoint for plugin installs."""

import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

# Checked before the package loads, which needs a newer Python than this
# script does. Everything above this line runs on any Python 3.
from agent_efficiency.python_support import unsupported_python_message  # noqa: E402

_UNSUPPORTED = unsupported_python_message(sys.version_info)
if _UNSUPPORTED:
    sys.stderr.write(_UNSUPPORTED + "\n")
    raise SystemExit(1)

from agent_efficiency.cli import main  # noqa: E402


if __name__ == "__main__":
    raise SystemExit(main())
