"""Tell a standing dev3 assistant agent about newly queued research (optional).

With `assistant_task` set, each application with new research or CV fit requests is sent
`/research <app id>` through `dev3 message`, so that agent starts at once in a pane the user
can watch. The command is a fixed argument list built from database ids only (no shell, no
form text). Any failure is logged and ignored: the requests stay queued for the next session
(SessionStart hook, /today).
"""

import logging
import shutil
import subprocess
import threading
from pathlib import Path

from prep.config import REPO_ROOT, Settings

log = logging.getLogger("prep.assistant")
TIMEOUT = 15


DEFAULT_DEV3 = Path.home() / ".dev3.0" / "bin" / "dev3"  # where dev3 installs its CLI


def dev3_path(settings: Settings) -> str | None:
    """settings.dev3_bin, else dev3 on PATH, else dev3's default install path (launchd's PATH
    is short, so the dashboard may not find it on PATH)."""
    if settings.dev3_bin:
        return str(settings.dev3_bin) if Path(settings.dev3_bin).is_file() else None
    return shutil.which("dev3") or (str(DEFAULT_DEV3) if DEFAULT_DEV3.is_file() else None)


def commands(settings: Settings, requests: list[dict], dev3: str) -> list[list[str]]:
    """One `dev3 message` argument list per application in `requests` (dicts with application_id)."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    app_ids = sorted({int(r["application_id"]) for r in requests})
    return [[dev3, "message", "--task", f"seq:{int(settings.assistant_task)}", *project,
             "--subject", f"Research queued for application {app_id}", f"/research {app_id}"]
            for app_id in app_ids]


def notify(settings: Settings, requests: list[dict]) -> int:
    """Send the messages; returns how many were delivered. Never raises."""
    if not settings.assistant_task or not requests:
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("assistant_task is set but the dev3 CLI was not found (set dev3_bin); requests stay queued")
        return 0
    sent = 0
    for argv in commands(settings, requests, dev3):
        try:
            result = subprocess.run(argv, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("dev3 message to the assistant failed (%s); requests stay queued", type(e).__name__)
            continue
        if result.returncode == 0:
            sent += 1
        else:
            log.warning("dev3 message to the assistant exited %s; requests stay queued", result.returncode)
    return sent


def notify_in_background(settings: Settings, requests: list[dict]) -> threading.Thread | None:
    """notify() in a daemon thread, so a dashboard request never waits for dev3."""
    if not settings.assistant_task or not requests:
        return None
    thread = threading.Thread(target=notify, args=(settings, list(requests)), daemon=True)
    thread.start()
    return thread
