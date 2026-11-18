"""Run the adjacent bundled runtime without installing Python packages."""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from agent_efficiency.pi_bridge import main  # noqa: E402

raise SystemExit(main())
