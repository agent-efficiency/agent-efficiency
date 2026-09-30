"""Read one hook payload on stdin and write the host response on stdout.

The plugin's hook script calls this, and ``python -m agent_efficiency.hook_entry``
runs it from an installed package, which is how smoke-test exercises a wheel.
"""

from __future__ import annotations

import json
import sys

from agent_efficiency.hook import run_hook


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
