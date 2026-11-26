"""The job criteria file: gates and ranking for the search (Top 5 this week).

The file is private Markdown (`criteria_file`, default private_dir/notes/criteria.md; never committed).
`criteria.example.md` in the repo shows the shape with made-up values. The code reads only what plain
code can use; the agent reads the whole file for the judgement calls (role family, level from the ad,
team size, pay in the ad):

    ## Gates ...                       numbered items "N. **Name**: text"; a job failing one is not shown
        Company, Sector, Interviews, Role family, Level, Team, Location, Pay (known names; a gate left
        out of the file is off, an unknown name is for the agent only)
        Level's "Out: a, b, c." list   title words that fail the level gate in plain code
        Location's "<City> on-site or hybrid" and "remote within the <Region>"
    ## Ranking ...                     numbered items, best first; each is matched to a signal by its words
        (evals, verdict, fit score, permanent, product company, fresh posting "under N weeks", agents)

The pay gate's figure is never read here: only the agent compares it with the ad, and only pass / fail
/ unknown is stored (prep/search.py).
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from prep.config import Settings

DEFAULT_FILE = Path("notes") / "criteria.md"
GATE_NAMES = {"company": "company", "sector": "sector", "interviews": "interviews", "interview": "interviews",
              "role family": "role_family", "role": "role_family", "level": "level", "team": "team",
              "location": "location", "pay": "pay"}
# Ranking signals, matched in this order to each ranking item's words (evals before agents: an evals item
# may mention agents too).
RANK_WORDS = (("evals", ("eval", "guardrail")), ("verdict", ("verdict",)), ("fit", ("fit score", "cv fit")),
              ("permanent", ("permanent", "contract")), ("product", ("product company", "consultancy")),
              ("fresh", ("fresh", "posted", "posting")), ("builds_agents", ("agent", "llm product")))
DEFAULT_FRESH_DAYS = 14
_ITEM = re.compile(r"^\s*(\d+)\.\s+(.*)$")
_BOLD = re.compile(r"^\*\*(.+?)\*\*:?\s*(.*)$")


class CriteriaMissing(Exception):
    """No criteria file: the Top 5 view says so and the plain New offers list is shown."""


@dataclass(frozen=True)
class Criteria:
    path: Path
    gates: tuple[str, ...]                       # known gate keys, in the file's order
    gate_text: dict[str, str] = field(default_factory=dict)
    level_out: tuple[str, ...] = ()              # title words that fail the level gate
    cities: tuple[str, ...] = ()                 # "London" from "London on-site or hybrid"
    remote_regions: tuple[str, ...] = ()         # "UK" from "remote within the UK"
    ranking: tuple[str, ...] = ()                # signal keys, best first
    fresh_days: int = DEFAULT_FRESH_DAYS


def criteria_path(settings: Settings) -> Path | None:
    """`criteria_file` when set, else private_dir/notes/criteria.md (None without either)."""
    if settings.criteria_file:
        return settings.criteria_file
    return settings.private_dir / DEFAULT_FILE if settings.private_dir else None


def load(settings: Settings) -> Criteria:
    """The criteria from the configured file. Raises CriteriaMissing when there is none."""
    path = criteria_path(settings)
    if path is None or not path.is_file():
        raise CriteriaMissing(f"no criteria file at {path}" if path else "private_dir is not set")
    return parse(path.read_text(encoding="utf-8"), path)


def _sections(text: str) -> dict[str, list[str]]:
    """Lines under each "## " heading, by the heading's lowercase text."""
    out: dict[str, list[str]] = {}
    current = None
    for line in text.splitlines():
        if line.startswith("## "):
            current = line[3:].strip().lower()
            out[current] = []
        elif current is not None:
            out[current].append(line)
    return out


def _items(lines: list[str]) -> list[str]:
    """Numbered list items, continuation lines joined to their item."""
    items: list[str] = []
    for line in lines:
        found = _ITEM.match(line)
        if found:
            items.append(found.group(2).strip())
        elif items and line.startswith((" ", "\t")) and line.strip():
            items[-1] += " " + line.strip()
        elif not line.strip() or not items:
            continue
        else:
            break  # a paragraph after the list ends it
    return items


def _section(sections: dict[str, list[str]], word: str) -> list[str]:
    return next((lines for heading, lines in sections.items() if word in heading), [])


def parse(text: str, path: Path) -> Criteria:
    """Criteria from the file's text (see the module docstring for what is read)."""
    sections = _sections(text)
    gates: list[str] = []
    gate_text: dict[str, str] = {}
    for item in _items(_section(sections, "gate")):
        found = _BOLD.match(item)
        name, body = (found.group(1), found.group(2)) if found else (item.split(":")[0], item)
        key = GATE_NAMES.get(name.strip().lower())
        if key and key not in gates:
            gates.append(key)
            if key != "pay":  # the pay figure is never kept: only the agent reads it, from the file
                gate_text[key] = body
    level_out = ()
    found = re.search(r"\bout:\s*([^.]*)", gate_text.get("level", ""), re.IGNORECASE)
    if found:
        level_out = tuple(w.strip().lower() for w in found.group(1).split(",") if w.strip())
    where = gate_text.get("location", "")
    cities = tuple(dict.fromkeys(re.findall(r"\b([A-Z][a-z]+) on-site", where)))
    regions = tuple(dict.fromkeys(re.findall(r"remote within (?:the )?([A-Z][A-Za-z]+)", where)))
    ranking: list[str] = []
    fresh_days = DEFAULT_FRESH_DAYS
    for item in _items(_section(sections, "ranking")):
        low = item.lower()
        key = next((k for k, words in RANK_WORDS if any(w in low for w in words)), None)
        if key and key not in ranking:
            ranking.append(key)
        if key == "fresh":
            weeks = re.search(r"(\d+)\s*weeks?", low)
            days = re.search(r"(\d+)\s*days?", low)
            fresh_days = int(weeks.group(1)) * 7 if weeks else int(days.group(1)) if days else fresh_days
    return Criteria(path=path, gates=tuple(gates), gate_text=gate_text, level_out=level_out, cities=cities,
                    remote_regions=regions, ranking=tuple(ranking), fresh_days=fresh_days)
