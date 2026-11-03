"""Desktop shortcut that opens the morning view (`jr app`)."""

from __future__ import annotations

import platform
import shutil
import stat
import sys
from pathlib import Path


def _jr() -> str:
    return shutil.which("jr") or f'"{sys.executable}" -m jobradar.cli'


def create_shortcut(desktop: Path | None = None) -> Path:
    desktop = desktop or Path.home() / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    system = platform.system()
    if system == "Windows":
        path = desktop / "Job Radar.bat"
        path.write_text(f"@echo off\r\n{_jr()} app\r\n", encoding="utf-8")
    elif system == "Darwin":
        path = desktop / "Job Radar.command"
        path.write_text(f"#!/bin/sh\nexec {_jr()} app\n", encoding="utf-8")
    else:
        path = desktop / "job-radar.desktop"
        path.write_text(f"[Desktop Entry]\nType=Application\nName=Job Radar\nExec={_jr()} app\nTerminal=true\n",
                        encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return path
