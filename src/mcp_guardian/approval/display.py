"""Discover a usable X11/Wayland display for popup windows."""

from __future__ import annotations

import glob
import os
from pathlib import Path


def ensure_display_env() -> bool:
    """
    Ensure DISPLAY (and preferably XAUTHORITY) are set.

    Claude Desktop often spawns MCP servers without a graphical environment.
    Returns True if a display appears available.
    """
    if not os.environ.get("DISPLAY"):
        # Prefer the first local X socket.
        sockets = sorted(Path("/tmp/.X11-unix").glob("X*"))
        if sockets:
            name = sockets[0].name  # e.g. X0
            os.environ["DISPLAY"] = f":{name[1:]}"
        else:
            os.environ["DISPLAY"] = ":0"

    if not os.environ.get("XAUTHORITY"):
        candidates: list[str] = []
        uid = os.getuid()
        candidates.extend(
            sorted(glob.glob(f"/run/user/{uid}/.mutter-Xwaylandauth.*"), reverse=True)
        )
        candidates.extend(
            sorted(glob.glob(f"/run/user/{uid}/.Xauthority"), reverse=True)
        )
        home_xa = Path.home() / ".Xauthority"
        if home_xa.is_file():
            candidates.append(str(home_xa))
        for path in candidates:
            if Path(path).is_file():
                os.environ["XAUTHORITY"] = path
                break

    return bool(os.environ.get("DISPLAY"))
