"""Retake the docs screenshots from the current build, on made-up data only.

Run from the repo root: `python scripts/screenshots.py [NAME ...]` (no names: every page-level shot).

It seeds the example databases (`prep.seed_example`), starts the dashboard on a spare port with an
empty settings file (so no real settings, private files, assistants or workers are read: the agent
pings go to a dev3 that does not exist), opens each page in headless Chrome and writes
docs/screenshots/NAME.png. Shots that need a click first (an open popover, a filled form, a selection)
are listed at the end to retake by hand.

Chrome: the macOS default path, or set CHROME to the binary.
"""

import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "docs" / "screenshots"
CHROME = os.environ.get("CHROME", "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")
EVAL_RESULTS = ROOT / "data" / "example-getjo-repo" / "evals" / "results"


def _latest(prefix: str) -> str:
    files = sorted(EVAL_RESULTS.glob(f"{prefix}-*.json"))
    return files[-1].name if files else ""


def _invite_app() -> int:
    """The example application recorded from a recruiter's invitation (prep.seed_example's add_invite_example)."""
    import sqlite3
    with sqlite3.connect(ROOT / "data" / "example.db") as conn:
        row = conn.execute("SELECT application_id FROM invitations WHERE application_id IS NOT NULL").fetchone()
    return row[0] if row else 0


def shots() -> dict[str, tuple[str, int, int]]:
    """name -> (path and query, width, height). Page-level only: no clicks needed."""
    return {
        "applications": ("/", 1440, 950),
        "new-offers": ("/?view=offers", 1440, 950),
        "linkedin-invitation": (f"/?app={_invite_app()}", 1440, 950),
        "pipeline-stats": ("/stats", 1440, 1300),
        "run-history": ("/runs", 1440, 950),
        "evals": ("/evals", 1440, 1500),
        "evals-history": ("/evals?eval=runpy-ci-gate", 1440, 1100),
        "evals-run": (f"/evals/run?file={_latest('runpy')}", 1440, 1800),
        "evals-promptfoo": (f"/evals/run?file={_latest('promptfoo')}", 1440, 1400),
    }


BY_HAND = {
    "learn": "a Learn card with its explainer and the floating chat open",
    "workers-sidebar": "the Workers sidebar with a working, an idle and a stuck worker",
    "workers-popover-stop": "a worker's popover with the Stop choice open",
    "draft-reply-form": "the Draft a reply form open and filled",
    "draft-linkedin": "a LinkedIn draft in the thread",
    "draft-email": "an email draft in the thread",
}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def example_env(tmp: Path) -> dict[str, str]:
    settings = tmp / "settings.toml"
    settings.write_text("# empty on purpose: screenshots read example data only\n", encoding="utf-8")
    (tmp / "private").mkdir()
    env = dict(os.environ)
    env.update({
        "INTERPREP_SETTINGS": str(settings),
        "PREP_DB": str(ROOT / "data" / "example.db"),
        "PREP_GETJO_DB": str(ROOT / "data" / "example-getjo.db"),
        "PREP_GETJO_REPO": str(ROOT / "data" / "example-getjo-repo"),
        "PREP_PRIVATE_DIR": str(tmp / "private"),
        "PREP_CRITERIA_FILE": str(ROOT / "criteria.example.md"),  # Top 5 on the example criteria
        "PREP_DEV3_BIN": "/nonexistent/dev3",
        "PREP_ASSISTANT_TASK": "", "PREP_PREP_ASSISTANT_TASK": "",
        "PREP_WORKER_RESEARCH": "", "PREP_WORKER_COMMS": "",
    })
    return env


def wait_for(url: str, seconds: float = 15) -> None:
    end = time.time() + seconds
    while time.time() < end:
        try:
            urllib.request.urlopen(url, timeout=1)
            return
        except OSError:
            time.sleep(0.3)
    raise SystemExit(f"the dashboard did not start at {url}")


def take(url: str, out: Path, width: int, height: int, profile: Path) -> None:
    subprocess.run([CHROME, "--headless=new", "--disable-gpu", "--hide-scrollbars", f"--user-data-dir={profile}",
                    "--virtual-time-budget=4000", f"--window-size={width},{height}", f"--screenshot={out}", url],
                   check=True, capture_output=True)


def main(names: list[str]) -> int:
    if not Path(CHROME).exists():
        print(f"Chrome not found at {CHROME}; set CHROME to its binary.")
        return 1
    subprocess.run([sys.executable, "-m", "prep.seed_example"], cwd=ROOT, check=True, capture_output=True)
    wanted = shots()
    unknown = [n for n in names if n not in wanted and n not in BY_HAND]
    if unknown:
        print(f"Unknown: {', '.join(unknown)}. Page-level: {', '.join(wanted)}.")
        return 1
    with tempfile.TemporaryDirectory() as tmp_name:
        tmp = Path(tmp_name)
        port = free_port()
        server = subprocess.Popen([sys.executable, "-m", "prep.web", "--port", str(port)], cwd=ROOT,
                                  env=example_env(tmp), stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            wait_for(f"http://127.0.0.1:{port}/")
            for name, (path, width, height) in wanted.items():
                if names and name not in names:
                    continue
                take(f"http://127.0.0.1:{port}{path}", OUT / f"{name}.png", width, height, tmp / "chrome")
                print(f"wrote docs/screenshots/{name}.png")
        finally:
            server.terminate()
            server.wait(timeout=10)
    by_hand = [n for n in BY_HAND if not names or n in names]
    if by_hand:
        print("\nRetake by hand (they need a click first), same example setup:")
        for name in by_hand:
            print(f"- {name}.png: {BY_HAND[name]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
