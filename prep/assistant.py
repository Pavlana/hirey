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

Routing (plain code, no LLM): ROUTES maps each kind of queued work to a [workers] route; when that
route has a worker task set (Settings.workers), the ping goes to the worker instead of `assistant_task`.
Today `research` takes company research, CV fit, tailored CVs, AI CV checks and job briefs, and `comms` the
"Draft a reply" requests (`/reply <app id | job | general> <message id>`, written by the /reply skill). Every
other thread message (/ask) and anything unmapped stays with the Applications Assistant, which also gets
the /reply pings when no comms worker is set.

A route may list several workers (`research = [71, 72]`): pick() sends each new item to the first one
with nothing claimed or waiting, else round-robin, and the dashboard records that choice on the item
(assign_requests, assign_briefs, assign_message) before the ping, so each worker's `research next` takes
its own work first. Claims stay atomic, so two workers never take the same item.

Each message ping's time and outcome is kept in memory (last_ping), so the dashboard can offer
Resend at once after a failed ping, or after RESEND_AFTER seconds without a reply.
"""

import logging
import sqlite3
import shutil
import subprocess
import threading
import time
from pathlib import Path

from prep import db
from prep.config import REPO_ROOT, Settings, worker_task, worker_tasks

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


APPLICATIONS_ASSISTANT = "Applications Assistant"
# Work kind -> [workers] route. Kinds not listed (thread messages) always go to assistant_task.
ROUTES = {"research": "research", "cv_fit": "research", "plan": "research", "tailor": "research", "cv_eval": "research",
          "brief": "research", "comms": "comms"}
WORKER_NAMES = {"research": "Research worker", "comms": "Comms worker"}


_TURNS: dict[str, int] = {}  # route -> the last round-robin turn, this process only
_TURNS_LOCK = threading.Lock()


def pick(settings: Settings, conn: sqlite3.Connection, route: str, exclude: tuple[int, ...] = ()) -> int | None:
    """The worker seq on `route` that gets the next item: the first with no claimed or assigned waiting
    item, else the next in turn. `exclude` is skipped while another is left (Stop's Back to queue)."""
    seqs = [s for s in worker_tasks(settings, route) if s not in exclude] or list(worker_tasks(settings, route))
    if len(seqs) < 2:
        return seqs[0] if seqs else None
    busy = db.busy_workers(conn)
    idle = next((s for s in seqs if s not in busy), None)
    if idle is not None:
        return idle
    with _TURNS_LOCK:
        turn = _TURNS[route] = _TURNS.get(route, -1) + 1
    return seqs[turn % len(seqs)]


def route_requests(settings: Settings, conn: sqlite3.Connection, requests: list[dict],
                   exclude: tuple[int, ...] = ()) -> list[dict]:
    """Choose a worker for each application's new requests (one per application, so its research, fit and
    tailoring stay together), record it on the requests and in each dict's "worker". Requests whose route
    has no worker are left to target(). Returns `requests`."""
    chosen: dict[tuple[int, str], int | None] = {}
    for r in requests:
        route = ROUTES.get(r.get("kind", "research"))
        if not route or not worker_tasks(settings, route):
            continue
        key = (int(r["application_id"]), route)
        if key not in chosen:
            chosen[key] = pick(settings, conn, route, exclude)
            db.assign_requests(conn, [x["id"] for x in requests if int(x["application_id"]) == key[0]
                                      and ROUTES.get(x.get("kind", "research")) == route], chosen[key])
        r["worker"] = chosen[key]
    return requests


def target(settings: Settings, kind: str) -> tuple[int | None, str]:
    """(dev3 task seq, name shown in the dashboard) that handles this kind of work: its worker when
    one is set, else the Applications Assistant (seq None when assistant_task is unset too)."""
    route = ROUTES.get(kind)
    seq = worker_task(settings, route) if route else None
    if seq:
        return seq, WORKER_NAMES.get(route, f"{route.capitalize()} worker")
    return settings.assistant_task, APPLICATIONS_ASSISTANT


DEFAULT_DEV3 = Path.home() / ".dev3.0" / "bin" / "dev3"  # where dev3 installs its CLI


def dev3_path(settings: Settings) -> str | None:
    """settings.dev3_bin, else dev3 on PATH, else dev3's default install path (launchd's PATH
    is short, so the dashboard may not find it on PATH)."""
    if settings.dev3_bin:
        return str(settings.dev3_bin) if Path(settings.dev3_bin).is_file() else None
    return shutil.which("dev3") or (str(DEFAULT_DEV3) if DEFAULT_DEV3.is_file() else None)


def _subject(kinds: set[str]) -> str:
    """Tailored CV, Tailoring plan or AI CV check when that is all that was queued, else Research."""
    return {frozenset({"tailor"}): "Tailored CV", frozenset({"cv_eval"}): "AI CV check",
            frozenset({"plan"}): "Tailoring plan"}.get(frozenset(kinds), "Research")


def commands(settings: Settings, requests: list[dict], dev3: str) -> list[list[str]]:
    """One `dev3 message` argument list per application and target task in `requests` (dicts with
    application_id and kind); requests whose target has no task set are left out."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    kinds: dict[tuple[int, int], set[str]] = {}
    for r in requests:
        kind = r.get("kind", "research")
        seq = r.get("worker") or target(settings, kind)[0]
        if seq:
            kinds.setdefault((int(r["application_id"]), int(seq)), set()).add(kind)
    return [[dev3, "message", "--task", f"seq:{seq}", *project,
             "--subject", f"{_subject(kinds[(app_id, seq)])} queued for application {app_id}",
             f"/research {app_id}"]
            for app_id, seq in sorted(kinds)]


def ask_command(settings: Settings, app_id: int | None, message_id: int, dev3: str, job: bool = False,
                topic: str | None = None, kind: str = "ask", seq: int | None = None) -> list[str]:
    """The `dev3 message` argument list for a new message: ids only, never its text. A message about
    a job offer not yet tracked (`job`) is sent as `/ask job <message id>` (the getjo job id stays out
    of the command), one about the master CV (`topic` "cv") as `/ask cv <message id>`, one about the
    whole search as `/ask general <message id>`. A draft request (`kind` "comms") is `/reply <app | job |
    general> <message id>` to whoever handles comms (target)."""
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
    if kind == "comms":
        about = about if about == "job" or about.isdigit() else "general"
        subject = {"job": "Draft a reply on a job offer", "general": "Draft a reply"}.get(
            about, f"Draft a reply on application {about}")
        return [dev3, "message", "--task", f"seq:{int(seq or target(settings, 'comms')[0])}", *project,
                "--subject", subject, f"/reply {about} {int(message_id)}"]
    return [dev3, "message", "--task", f"seq:{int(settings.assistant_task)}", *project,
            "--subject", subject, f"/ask {about} {int(message_id)}"]


def brief_command(settings: Settings, count: int, dev3: str, seq: int | None = None) -> list[str]:
    """The `dev3 message` argument list for newly queued job briefs: a count and no job ids (`brief next`
    lists them). `seq`: the worker pick() chose, else whoever handles briefs."""
    project = ["--project", settings.assistant_project] if settings.assistant_project else []
    return [dev3, "message", "--task", f"seq:{int(seq or target(settings, 'brief')[0])}", *project,
            "--subject", f"{int(count)} job brief{'s' if count != 1 else ''} queued", "/research briefs"]


def notify_briefs(settings: Settings, count: int, seq: int | None = None) -> int:
    """Send `/research briefs` to `seq` or whoever handles briefs; returns 1 if delivered. Never raises."""
    if not target(settings, "brief")[0] or count < 1:
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("a task for briefs is set but the dev3 CLI was not found (set dev3_bin); briefs stay queued")
        return 0
    return _send([brief_command(settings, count, dev3, seq)])


def notify_briefs_in_background(settings: Settings, count: int, seq: int | None = None) -> threading.Thread | None:
    """notify_briefs() in a daemon thread, so a page load never waits for dev3."""
    if not target(settings, "brief")[0] or count < 1:
        return None
    thread = threading.Thread(target=notify_briefs, args=(settings, count, seq), daemon=True)
    thread.start()
    return thread


def notify(settings: Settings, requests: list[dict]) -> int:
    """Send /research messages for these requests, each to its target task; returns how many were
    delivered. Never raises."""
    if not _routed(settings, requests):
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("a task for research is set but the dev3 CLI was not found (set dev3_bin); requests stay queued")
        return 0
    return _send(commands(settings, requests, dev3))


def _routed(settings: Settings, requests: list[dict]) -> bool:
    """True when at least one request has a task to ping."""
    return any(target(settings, r.get("kind", "research"))[0] for r in requests)


def message_target(settings: Settings, kind: str = "ask") -> int | None:
    """The dev3 task seq a message ping goes to: the comms route for a draft request, else assistant_task."""
    return target(settings, "comms")[0] if kind == "comms" else settings.assistant_task


def notify_message(settings: Settings, app_id: int | None, message_id: int, job: bool = False,
                   topic: str | None = None, kind: str = "ask", seq: int | None = None) -> int:
    """Send `/ask <app id | job | cv | general> <message id>` (or `/reply …` for a draft request, `kind`
    "comms"); returns 1 if delivered. Never raises."""
    if not message_target(settings, kind):
        return 0
    dev3 = dev3_path(settings)
    if dev3 is None:
        log.warning("assistant_task is set but the dev3 CLI was not found (set dev3_bin); the message stays pending")
        return _record(message_id, 0)
    return _record(message_id, _send([ask_command(settings, app_id, message_id, dev3, job, topic, kind, seq)]))


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
    if not _routed(settings, requests):
        return None
    thread = threading.Thread(target=notify, args=(settings, list(requests)), daemon=True)
    thread.start()
    return thread


def notify_message_in_background(settings: Settings, app_id: int | None, message_id: int,
                                 job: bool = False, topic: str | None = None,
                                 kind: str = "ask", seq: int | None = None) -> threading.Thread | None:
    """notify_message() in a daemon thread, so the Send button never waits for dev3."""
    if not message_target(settings, kind):
        return None
    thread = threading.Thread(target=notify_message, args=(settings, app_id, message_id, job, topic, kind, seq),
                              daemon=True)
    thread.start()
    return thread
