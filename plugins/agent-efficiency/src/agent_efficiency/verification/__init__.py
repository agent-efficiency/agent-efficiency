"""Project verification contracts and receipt handling."""

from agent_efficiency.verification.config import ProjectConfig, load_project_config
from agent_efficiency.verification.runner import run_checks

__all__ = ["ProjectConfig", "load_project_config", "run_checks"]
