"""Tell a standing dev3 assistant agent about new work (optional).

With `assistant_task` set, each application with new research, CV fit or tailored CV requests is sent
`/research <app id>`, and each new message in the Applications Assistant box `/ask <app id> <message id>` (or
`/ask job <message id>` for a job offer not yet tracked, `/ask cv <message id>` for the master CV,
`/ask compare <message id>` for a question about several ticked jobs, `/ask general <message id>`
for the whole search), and new job briefs `/research briefs`, through `dev3 message`, so that agent starts at once in a pane the user can watch. The
command is a fixed argument list built from database ids only (no shell, never form or message
text). Any failure is logged and ignored: the work stays queued for the next session
(SessionStart hook, /today).

With `prep_assistant_task` set, each new message to the Learn page's Learn Assistant is sent to that second
assistant as `/learn-ask <card id> <message id>` (or `/learn-ask general <message id>` with no card).

Each message ping's time and outcome is kept in memory (last_ping), so the dashboard can offer
Resend at once after a failed ping, or after RESEND_AFTER seconds without a reply.
"""

import logging
import shutil
import subprocess
import threading
import time
from pathlib import Path

from prep.config import REPO_ROOT, Settings

log = logging.getLogger("prep.assistant")
TIMEOUT = 15
RESEND_AFTER = 30  # seconds a message waits before Resend shows (at once when its ping failed)
_PINGS: dict[int, tuple[float, bool]] = {}  # message id -> (when, delivered), this process's pings only
_PINGS_LOCK = threading.Lock()


def _record(message_id: int, sent: int) -> int:
    with _PINGS_LOCK:
        _PINGS[int(message_id)] = (time.time(), sent > 0)
    return sent


def last_ping(message_id: int) -> tuple[float, bool] | None:
    """(when, delivered) of the latest ping about a message since the dashboard started, or None."""
    with _PINGS_LOCK:
        return _PINGS.get(int(message_id))


DEFAULT_DEV3 = Path.home() / ".dev3.0" / "bin" / "dev3"  # where dev3 installs its CLI


def dev3_path(settings: Settings) -> str | None:
    """settings.dev3_bin, else dev3 on PATH, else dev3's default install path (launchd's PATH
    is short, so the dashboard may not find it on PATH)."""
    if settings.dev3_bin:
        return str(settings.dev3_bin) if Path(settings.dev3_bin).is_file() else None
    return shutil.which("dev3") or (str(DEFAULT_DEV3) if DEFAULT_DEV3.is_file() else None)


def _subject(kinds: set[str]) -> str:
    """Tailored CV or AI CV check when that is all that was queued, else Research."""
    return {frozenset({"tailor"}): "Tailored CV", frozenset({"cv_eval"}): "AI CV check"}.get(frozenset(kinds), "Research")


def commands(settings: Settings, requests: list[dict], dev3: str) -> list[list[str]]:
    """One `dev3 message` argument list per application in `requests` (dicts with application_id)."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    kinds: dict[int, set[str]] = {}
    for r in requests:
        kinds.setdefault(int(r["application_id"]), set()).add(r.get("kind", "research"))
    return [[dev3, "message", "--task", f"seq:{int(settings.assistant_task)}", *project,
             "--subject", f"{_subject(kinds[app_id])} queued for application {app_id}",
             f"/research {app_id}"]
            for app_id in sorted(kinds)]


def ask_command(settings: Settings, app_id: int | None, message_id: int, dev3: str, job: bool = False,
                topic: str | None = None) -> list[str]:
    """The `dev3 message` argument list for a new message: ids only, never its text. A message about
    a job offer not yet tracked (`job`) is sent as `/ask job <message id>` (the getjo job id stays out
    of the command), one about the master CV (`topic` "cv") as `/ask cv <message id>`, one about the
    whole search as `/ask general <message id>`."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    if app_id is not None:
        about, subject = str(int(app_id)), f"Question on application {int(app_id)}"
    elif job:
        about, subject = "job", "Question on a job offer"
    elif topic == "cv":
        about, subject = "cv", "Request about the master CV"
    elif topic == "compare":
        about, subject = "compare", "Question comparing several jobs"
    else:
        about, subject = "general", "Question on the whole search"
    return [dev3, "message", "--task", f"seq:{int(settings.assistant_task)}", *project,
            "--subject", subject, f"/ask {about} {int(message_id)}"]


def brief_command(settings: Settings, count: int, dev3: str) -> list[str]:
    """The `dev3 message` argument list for newly queued job briefs: a count and no job ids (`brief next`
    lists them)."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    return [dev3, "message", "--task", f"seq:{int(settings.assistant_task)}", *project,
            "--subject", f"{int(count)} job brief{'s' if count != 1 else ''} queued", "/research briefs"]


def notify_briefs(settings: Settings, count: int) -> int:
    """Send `/research briefs`; returns 1 if delivered. Never raises."""
    if not settings.assistant_task or count < 1:
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("assistant_task is set but the dev3 CLI was not found (set dev3_bin); briefs stay queued")
        return 0
    return _send([brief_command(settings, count, dev3)])


def notify_briefs_in_background(settings: Settings, count: int) -> threading.Thread | None:
    """notify_briefs() in a daemon thread, so a page load never waits for dev3."""
    if not settings.assistant_task or count < 1:
        return None
    thread = threading.Thread(target=notify_briefs, args=(settings, count), daemon=True)
    thread.start()
    return thread


def notify(settings: Settings, requests: list[dict]) -> int:
    """Send /research messages for these requests; returns how many were delivered. Never raises."""
    if not settings.assistant_task or not requests:
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("assistant_task is set but the dev3 CLI was not found (set dev3_bin); requests stay queued")
        return 0
    return _send(commands(settings, requests, dev3))


def notify_message(settings: Settings, app_id: int | None, message_id: int, job: bool = False,
                   topic: str | None = None) -> int:
    """Send `/ask <app id | job | cv | general> <message id>`; returns 1 if delivered. Never raises."""
    if not settings.assistant_task:
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("assistant_task is set but the dev3 CLI was not found (set dev3_bin); the message stays pending")
        return _record(message_id, 0)
    return _record(message_id, _send([ask_command(settings, app_id, message_id, dev3, job, topic)]))


def learn_command(settings: Settings, card_id: int | None, message_id: int, dev3: str) -> list[str]:
    """The `dev3 message` argument list for a new Learn Assistant message: ids only, never its text."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    about = str(int(card_id)) if card_id is not None else "general"
    subject = f"Learn question on card {int(card_id)}" if card_id is not None else "Learn question"
    return [dev3, "message", "--task", f"seq:{int(settings.prep_assistant_task)}", *project,
            "--subject", subject, f"/learn-ask {about} {int(message_id)}"]


def notify_learn(settings: Settings, card_id: int | None, message_id: int) -> int:
    """Send `/learn-ask <card | general> <message id>` to the Learn Assistant; returns 1 if delivered.
    Never raises."""
    if not settings.prep_assistant_task:
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("prep_assistant_task is set but the dev3 CLI was not found (set dev3_bin); the message stays pending")
        return _record(message_id, 0)
    return _record(message_id, _send([learn_command(settings, card_id, message_id, dev3)]))


def notify_learn_in_background(settings: Settings, card_id: int | None, message_id: int) -> threading.Thread | None:
    """notify_learn() in a daemon thread, so the Send button never waits for dev3."""
    if not settings.prep_assistant_task:
        return None
    thread = threading.Thread(target=notify_learn, args=(settings, card_id, message_id), daemon=True)
    thread.start()
    return thread


def _send(argvs: list[list[str]]) -> int:
    sent = 0
    for argv in argvs:
        try:
            result = subprocess.run(argv, cwd=REPO_ROOT, stdin=subprocess.DEVNULL, capture_output=True,
                                    timeout=TIMEOUT, check=False)
        except (OSError, subprocess.SubprocessError) as e:
            log.warning("dev3 message to the assistant failed (%s); the work stays queued", type(e).__name__)
            continue
        if result.returncode == 0:
            sent += 1
        else:
            log.warning("dev3 message to the assistant exited %s; the work stays queued", result.returncode)
    return sent


def notify_in_background(settings: Settings, requests: list[dict]) -> threading.Thread | None:
    """notify() in a daemon thread, so a dashboard request never waits for dev3."""
    if not settings.assistant_task or not requests:
        return None
    thread = threading.Thread(target=notify, args=(settings, list(requests)), daemon=True)
    thread.start()
    return thread


def notify_message_in_background(settings: Settings, app_id: int | None, message_id: int,
                                 job: bool = False, topic: str | None = None) -> threading.Thread | None:
    """notify_message() in a daemon thread, so the Send button never waits for dev3."""
    if not settings.assistant_task:
        return None
    thread = threading.Thread(target=notify_message, args=(settings, app_id, message_id, job, topic), daemon=True)
    thread.start()
    return thread
