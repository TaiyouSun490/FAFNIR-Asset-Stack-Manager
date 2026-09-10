"""Shared local transport location, independent of catalog services."""
import os
from pathlib import Path


def default_bridge_root() -> Path:
    override = os.getenv("FAFNIR_BRIDGE_ROOT")
    if override:
        path = Path(override).expanduser()
        if not path.is_absolute():
            raise ValueError("FAFNIR_BRIDGE_ROOT must be an absolute directory.")
        return path
    base = os.getenv("LOCALAPPDATA") or os.getenv("APPDATA")
    root = Path(base) if base else Path.home() / ".local" / "share"
    return root / "game-stack-planner"
