#!/usr/bin/env python3
"""Self-contained lifecycle-hook entrypoint."""

from __future__ import annotations

import json
import sys
from pathlib import Path


PLUGIN_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PLUGIN_ROOT / "src"))

from agent_efficiency.hook import run_hook  # noqa: E402


def main() -> int:
    try:
        payload = json.load(sys.stdin)
        output = run_hook(payload)
        if output:
            json.dump(output, sys.stdout, separators=(",", ":"))
            sys.stdout.write("\n")
    except Exception:
        # Lifecycle hooks are an efficiency aid, never an execution boundary.
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
