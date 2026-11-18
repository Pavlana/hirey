"""Spaced repetition for Learn cards: SM-2 (the classic SuperMemo 2 rules), as pure functions.

The four buttons map to SM-2 grades (0-5):

    again -> 2   wrong: the card starts over (interval 1 day, reps 0, one more lapse)
    hard  -> 3   right, with effort
    good  -> 4   right
    easy  -> 5   right, at once

Good follows SM-2: the interval goes 1 day, 6 days, then the previous interval times the ease
(rounded half up). Again restarts the card at 1 day. Pure SM-2 gives Hard and Easy the same
interval as Good, so the four buttons would preview the same day; like Anki, this "SM-2-style"
version spreads them instead:

    hard  1 day while reps is 0, else max(previous + 1, previous * 1.2), never more than Good
    easy  4 days while reps is 0, else max(Good + 1, Good * 1.3)

Every rating then changes the ease by SM-2's 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02): easy +0.10,
good 0, hard -0.14, again -0.32, never below 1.3. So Again <= Hard <= Good < Easy in every state.
"""

from dataclasses import dataclass, replace
from datetime import date, timedelta

RATINGS = ("again", "hard", "good", "easy")
GRADES = {"again": 2, "hard": 3, "good": 4, "easy": 5}
START_EASE = 2.5
MIN_EASE = 1.3


@dataclass(frozen=True)
class CardState:
    ease: float = START_EASE
    interval_days: int = 0
    reps: int = 0      # right answers in a row since the last lapse
    lapses: int = 0


def grade(rating: str) -> int:
    if rating not in GRADES:
        raise ValueError(f"rating must be one of {', '.join(RATINGS)}; got {rating!r}")
    return GRADES[rating]


def next_state(state: CardState, rating: str) -> CardState:
    """The card's state after one review rated `rating`."""
    q = grade(rating)
    if q >= 3:
        good = _good_interval(state)
        if rating == "hard":
            interval = 1 if state.reps == 0 else min(good, max(state.interval_days + 1, _round(state.interval_days * 1.2)))
        elif rating == "easy":
            interval = 4 if state.reps == 0 else max(good + 1, _round(good * 1.3))
        else:
            interval = good
        new = replace(state, interval_days=interval, reps=state.reps + 1)
    else:
        new = replace(state, interval_days=1, reps=0, lapses=state.lapses + 1)
    ease = state.ease + 0.1 - (5 - q) * (0.08 + (5 - q) * 0.02)
    return replace(new, ease=round(max(MIN_EASE, ease), 2))


def _round(x: float) -> int:
    return int(x + 0.5)  # half up, not Python's half to even


def _good_interval(state: CardState) -> int:
    """SM-2's interval for a right answer."""
    if state.reps == 0:
        return 1
    if state.reps == 1:
        return 6
    return _round(state.interval_days * state.ease)


def due_date(today: str, interval_days: int) -> str:
    return (date.fromisoformat(today) + timedelta(days=interval_days)).isoformat()


def preview(state: CardState, today: str) -> dict[str, tuple[int, str]]:
    """For each button: the next interval in days and the due date it gives ("Good → 6 days")."""
    out = {}
    for rating in RATINGS:
        interval = next_state(state, rating).interval_days
        out[rating] = (interval, due_date(today, interval))
    return out


def interval_label(days: int) -> str:
    return "1 day" if days == 1 else f"{days} days"
