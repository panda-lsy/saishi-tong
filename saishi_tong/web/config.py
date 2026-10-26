"""Web configuration deliberately excludes SearchPro/CAM credentials.

Environment values take precedence. Files are parsed as data, never sourced or
interpolated; disallowed lines are discarded before their values are parsed.
"""
from __future__ import annotations

import os
from pathlib import Path

# Admin token plus the OpenClaw worker bearer token (server-side only).
WEB_KEYS = frozenset({"SAISHI_ADMIN_TOKEN", "SAISHI_WORKER_TOKEN"})
CAM_KEYS = ("TENCENTCLOUD_SECRET_ID", "TENCENTCLOUD_SECRET_KEY", "TENCENTCLOUD_TOKEN")


def _load_allowlisted(path: Path, allowed: frozenset[str]) -> None:
    try:
        with path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if line.startswith("export "):
                    line = line[7:].lstrip()
                name, separator, value = line.partition("=")
                name = name.strip()
                if not separator or name not in allowed:
                    continue
                value = value.strip()
                if len(value) >= 2 and value[0] in "\"'" and value[-1] == value[0]:
                    value = value[1:-1]
                else:
                    value = value.split(" #", 1)[0].rstrip()
                os.environ.setdefault(name, value)
    except FileNotFoundError:
        pass


def load_web_config() -> None:
    # Startup-only process isolation. The CLI's independent loader is unchanged.
    for name in CAM_KEYS:
        os.environ.pop(name, None)
    root = Path.home() / ".config" / "saishi-tong"
    _load_allowlisted(root / "tencent.env", WEB_KEYS)
