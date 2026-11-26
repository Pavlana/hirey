"""getjo's evals, read only: the catalogue (evals/catalog.json) and the results files in evals/results/.

Everything is read from `settings.getjo_repo`; nothing there is written, run or imported. Two result
shapes: evals.run's own (`runpy-<set>-<stamp>.json`, schema in getjo's docs/scoring.md section 11) and
promptfoo's (`promptfoo-<set>-<stamp>.json`, beside an `.html` report). Results hold private case data
(titles, companies, reasons): it is shown on the loopback dashboard only and never logged.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path

log = logging.getLogger(__name__)

CATALOG = Path("evals/catalog.json")
RESULTS = Path("evals/results")
STATUSES = ("built", "partial", "gap")
STAMP = r"\d{8}T\d{6}"
SET = r"[a-z0-9][a-z0-9_-]{0,40}"
# The only file names read or served from evals/results: runner, set, local time stamp, extension.
NAME_RE = re.compile(rf"^(runpy|promptfoo)-({SET})-({STAMP})\.(json|html)$")
PREFERRED = ("agreement", "verdict")  # the headline metric when an eval lists one of these
MAX_BYTES = 20_000_000  # a results file larger than this is skipped


# --- the catalogue ----------------------------------------------------------------------------

def load_catalog(repo: Path | None) -> dict:
    """{"state": ok | no_repo | missing | invalid, "about", "evals": [entry, ...], "path"}.
    Entries keep getjo's fields; unknown statuses become "gap" and lists default to empty."""
    if repo is None:
        return {"state": "no_repo", "about": "", "evals": [], "path": ""}
    path = Path(repo) / CATALOG
    if not path.is_file():
        return {"state": "missing", "about": "", "evals": [], "path": str(path)}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        evals = [_entry(e) for e in data["evals"] if isinstance(e, dict) and e.get("id")]
    except (OSError, ValueError, KeyError, TypeError) as e:
        log.warning("getjo eval catalogue unreadable (%s)", e.__class__.__name__)
        return {"state": "invalid", "about": "", "evals": [], "path": str(path)}
    return {"state": "ok", "about": str(data.get("about") or ""), "evals": evals, "path": str(path)}


def _strs(value: object) -> list[str]:
    return [str(v) for v in value] if isinstance(value, list) else []


def _entry(e: dict) -> dict:
    runs = e.get("runs") if isinstance(e.get("runs"), dict) else {}
    return {
        "id": str(e["id"]),
        "name": str(e.get("name") or e["id"]),
        "question": str(e.get("question") or ""),
        "dimensions": _strs(e.get("dimensions")),
        "grader": str(e.get("grader") or ""),
        "metrics": [m for m in e.get("metrics") or [] if isinstance(m, dict) and m.get("name")],
        "where": _strs(runs.get("where")),
        "when": str(runs.get("when") or ""),
        "command": str(e["command"]) if e.get("command") else "",
        "results": str(e["results"]) if e.get("results") else "",
        "how_to_check": _strs(e.get("how_to_check")),
        "if_fails": _strs(e.get("if_fails")),
        "code": [c for c in e.get("code") or [] if isinstance(c, dict) and c.get("file")],
        "status": e.get("status") if e.get("status") in STATUSES else "gap",
    }


def results_pattern(pattern: str) -> re.Pattern | None:
    """The catalogue's results pattern (evals/results/runpy-ci-<YYYYMMDDTHHMMSS>.json) as a regex on
    the file name; <set> matches any set name. None when the eval writes no results file."""
    if not pattern:
        return None
    name = Path(pattern).name
    regex = re.escape(name).replace(re.escape("<YYYYMMDDTHHMMSS>"), STAMP).replace(re.escape("<set>"), SET)
    return re.compile(f"^{regex}$")


# --- results files ----------------------------------------------------------------------------

def results_dir(repo: Path | None) -> Path | None:
    return None if repo is None else Path(repo) / RESULTS


def list_results(repo: Path | None) -> list[dict]:
    """The JSON results files, oldest first: {"name", "runner", "set", "stamp", "when", "html"}."""
    folder = results_dir(repo)
    if folder is None or not folder.is_dir():
        return []
    found = []
    for p in folder.iterdir():
        m = NAME_RE.match(p.name)
        if not m or m.group(4) != "json" or not p.is_file():
            continue
        runner, case_set, stamp, _ = m.groups()
        found.append({"name": p.name, "runner": runner, "set": case_set, "stamp": stamp, "when": _when(stamp),
                      "html": (folder / f"{p.stem}.html").is_file()})
    return sorted(found, key=lambda r: (r["stamp"], r["name"]))


def _when(stamp: str) -> str:
    """20261126T132316 -> 2026-11-26 13:23."""
    try:
        return datetime.strptime(stamp, "%Y%m%dT%H%M%S").strftime("%Y-%m-%d %H:%M")
    except ValueError:
        return stamp


def safe_path(repo: Path | None, name: str) -> Path | None:
    """The results file `name` inside evals/results, or None: the name must match NAME_RE exactly (no
    folders, no dots beyond the extension) and resolve inside the folder (no links out of it)."""
    folder = results_dir(repo)
    if folder is None or not isinstance(name, str) or not NAME_RE.match(name):
        return None
    try:
        base = folder.resolve(strict=True)
        path = (folder / name).resolve(strict=True)
    except (OSError, RuntimeError):
        return None
    if path.parent != base or not path.is_file():
        return None
    return path


def read_json(repo: Path | None, name: str) -> dict | None:
    path = safe_path(repo, name)
    if path is None or not name.endswith(".json"):
        return None
    try:
        if path.stat().st_size > MAX_BYTES:
            log.warning("eval results file too large, skipped: %s", name)
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        log.warning("eval results file unreadable (%s): %s", e.__class__.__name__, name)
        return None
    return data if isinstance(data, dict) else None


def read_html(repo: Path | None, name: str) -> bytes | None:
    """A saved promptfoo HTML report, or None."""
    path = safe_path(repo, name)
    if path is None or not name.startswith("promptfoo-") or not name.endswith(".html"):
        return None
    try:
        return path.read_bytes() if path.stat().st_size <= MAX_BYTES else None
    except OSError:
        return None


def summary(name: str, data: dict) -> dict:
    """One results file as a dict the page renders: runpy or promptfoo, by the file name."""
    return promptfoo_summary(data) if name.startswith("promptfoo-") else runpy_summary(data)


# --- evals.run results -----------------------------------------------------------------------

def _rate(value: object) -> dict:
    """{"hits", "total", "rate"} from run.py's rate shape; missing parts become 0 / None."""
    v = value if isinstance(value, dict) else {}
    hits, total = _num(v.get("hits")) or 0, _num(v.get("total")) or 0
    rate = _num(v.get("rate"))
    return {"hits": int(hits), "total": int(total), "rate": rate if rate is not None else (hits / total if total else None)}


def _num(value: object) -> float | None:
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def runpy_summary(data: dict) -> dict:
    cases = [c for c in data.get("cases") or [] if isinstance(c, dict)]
    confusion = data.get("confusion") if isinstance(data.get("confusion"), dict) else {}
    sweep = [{"threshold": s.get("threshold"), "sent": s.get("sent"), "precision": _rate(s.get("precision")),
              "recall": _rate(s.get("recall"))} for s in data.get("sweep") or [] if isinstance(s, dict)]
    metrics = {k: _rate(data.get(k)) for k in ("agreement", "apply_precision", "apply_recall")}
    metrics["personal_mentions"] = {"value": _num(data.get("personal_mentions"))}
    return {
        "runner": "runpy",
        "set": str(data.get("set") or ""),
        "created_at": str(data.get("created_at") or ""),
        "model": str(data.get("model") or ""),
        "models": [str(data["model"])] if data.get("model") else [],
        "threshold": data.get("threshold"),
        "commit": str(data.get("commit") or ""),
        "prompt_sha": str(data.get("prompt_sha") or ""),
        "metrics": metrics,
        "passed": data.get("passed") if isinstance(data.get("passed"), bool) else None,
        "min_agreement": data.get("min_agreement"),
        "confusion": confusion,
        "sweep": sweep,
        "cases": cases,
        "disagreements": [c for c in cases if not c.get("agree")],
    }


# --- promptfoo results -----------------------------------------------------------------------

def _label(provider: object) -> str:
    if isinstance(provider, dict):
        return str(provider.get("label") or provider.get("id") or "")
    return str(provider or "")


def _output(response: object) -> dict | None:
    """The scorer's JSON result from promptfoo's response.output (a JSON string, or "null")."""
    raw = response.get("output") if isinstance(response, dict) else None
    if isinstance(raw, dict):
        return raw
    try:
        data = json.loads(raw) if isinstance(raw, str) else None
    except ValueError:
        return None
    return data if isinstance(data, dict) else None


def promptfoo_summary(data: dict) -> dict:
    """Per model: pass/fail counts, each named metric's mean. Per case x model: pass, the named
    checks, the score and verdict, the judge's score and text."""
    body = data.get("results") if isinstance(data.get("results"), dict) else {}
    rows = [r for r in body.get("results") or [] if isinstance(r, dict)]
    models: dict[str, dict] = {}
    for p in body.get("prompts") or []:
        if not isinstance(p, dict):
            continue
        m = p.get("metrics") if isinstance(p.get("metrics"), dict) else {}
        named, counts = m.get("namedScores") or {}, m.get("namedScoresCount") or {}
        models[_label(p.get("provider"))] = {
            "label": _label(p.get("provider")),
            "pass": int(_num(m.get("testPassCount")) or 0),
            "fail": int(_num(m.get("testFailCount")) or 0),
            "error": int(_num(m.get("testErrorCount")) or 0),
            "latency_ms": _num(m.get("totalLatencyMs")),
            "named": {k: v / counts[k] for k, v in named.items()
                      if _num(v) is not None and _num(counts.get(k))} if isinstance(named, dict) else {},
        }
    cases: dict[int, dict] = {}
    for r in rows:
        model = _label(r.get("provider"))
        stats = models.setdefault(model, {"label": model, "pass": 0, "fail": 0, "error": 0,
                                          "latency_ms": None, "named": {}, "_derived": True})
        if stats.get("_derived"):
            key = "error" if r.get("error") and not r.get("success") else "pass" if r.get("success") else "fail"
            stats[key] += 1
        idx = int(_num(r.get("testIdx")) or 0)
        test_vars = r.get("vars") or (r.get("testCase") or {}).get("vars") or {}
        case = cases.setdefault(idx, {"idx": idx, "title": str(test_vars.get("title") or f"Case {idx + 1}"),
                                      "company": str(test_vars.get("company") or ""),
                                      "expected": str(test_vars.get("expected") or ""), "cells": {}})
        out = _output(r.get("response"))
        checks, judge = {}, None
        grading = r.get("gradingResult") if isinstance(r.get("gradingResult"), dict) else {}
        for c in grading.get("componentResults") or []:
            if not isinstance(c, dict):
                continue
            a = c.get("assertion") if isinstance(c.get("assertion"), dict) else {}
            metric = str(a.get("metric") or a.get("type") or "")
            if a.get("type") == "llm-rubric":
                judge = {"metric": metric, "score": _num(c.get("score")), "reason": str(c.get("reason") or "")}
            else:
                checks[metric] = {"pass": bool(c.get("pass")), "reason": str(c.get("reason") or "")}
        if not checks:
            checks = {k: {"pass": (_num(v) or 0) >= 1, "reason": ""}
                      for k, v in (r.get("namedScores") or {}).items() if judge is None or k != judge["metric"]}
        case["cells"][model] = {
            "pass": bool(r.get("success")),
            "error": str(r.get("error") or ""),
            "checks": checks,
            "judge": judge,
            "score": _num(out.get("score")) if out else None,
            "reasons": _strs(out.get("reasons")) if out else [],
            "latency_ms": _num(r.get("latencyMs")),
        }
    for stats in models.values():
        if stats.pop("_derived", False):
            stats["named"] = _named_means([c["cells"].get(stats["label"]) for c in cases.values()])
    labels = list(models)
    total_pass = sum(m["pass"] for m in models.values())
    total = sum(m["pass"] + m["fail"] + m["error"] for m in models.values())
    metrics: dict[str, dict] = {"pass_rate": {"hits": total_pass, "total": total,
                                              "rate": total_pass / total if total else None}}
    for name in {k for m in models.values() for k in m["named"]}:
        values = [m["named"][name] for m in models.values() if name in m["named"]]
        metrics[name] = {"rate": sum(values) / len(values) if values else None}
    return {
        "runner": "promptfoo",
        "set": "",
        "created_at": str(body.get("timestamp") or data.get("createdAt") or ""),
        "model": ", ".join(labels),
        "models": labels,
        "threshold": None,
        "commit": "",
        "prompt_sha": "",
        "metrics": metrics,
        "passed": None,
        "per_model": [models[k] for k in labels],
        "cases": [cases[k] for k in sorted(cases)],
    }


def _named_means(cells: list[dict | None]) -> dict[str, float]:
    sums: dict[str, list[float]] = {}
    for cell in cells:
        if not cell:
            continue
        for k, c in cell["checks"].items():
            sums.setdefault(k, []).append(1.0 if c["pass"] else 0.0)
        if cell["judge"] and cell["judge"]["score"] is not None:
            sums.setdefault(cell["judge"]["metric"], []).append(cell["judge"]["score"])
    return {k: sum(v) / len(v) for k, v in sums.items() if v}


# --- evals and their runs --------------------------------------------------------------------

def headline_metric(entry: dict, s: dict) -> str | None:
    """The metric a run of this eval is summed up by: agreement / verdict when the eval lists it, else
    the first listed metric the run has, else promptfoo's pass rate."""
    names = [m["name"] for m in entry["metrics"]]
    for name in [*[p for p in PREFERRED if p in names], *names]:
        if s["metrics"].get(name, {}).get("rate") is not None:
            return name
    return "pass_rate" if s["metrics"].get("pass_rate", {}).get("rate") is not None else None


def runs_for(entry: dict, files: list[dict]) -> list[dict]:
    """The results files that belong to this eval (its results pattern), oldest first."""
    pattern = results_pattern(entry["results"])
    return [f for f in files if pattern and pattern.match(f["name"])] if pattern else []


def history(repo: Path | None, entry: dict, files: list[dict]) -> list[dict]:
    """Each run of the eval, oldest first: the file, its summary and the headline value (0..1)."""
    rows = []
    for f in runs_for(entry, files):
        data = read_json(repo, f["name"])
        if data is None:
            continue
        s = summary(f["name"], data)
        metric = headline_metric(entry, s)
        value = s["metrics"][metric]["rate"] if metric else None
        rows.append({**f, "summary": s, "metric": metric, "value": value})
    return rows


def overview(repo: Path | None) -> dict:
    """The catalogue with each eval's runs: {"catalog", "files", "evals": [{**entry, "runs"}]}."""
    catalog = load_catalog(repo)
    files = list_results(repo) if catalog["state"] == "ok" else []
    evals = [{**e, "runs": history(repo, e, files)} for e in catalog["evals"]]
    return {"catalog": catalog, "files": files, "evals": evals}


def find(catalog: dict, eval_id: str) -> dict | None:
    return next((e for e in catalog["evals"] if e["id"] == eval_id), None)


def evals_for_file(catalog: dict, name: str) -> list[dict]:
    """The catalogue entries whose results pattern matches this file name."""
    out = []
    for e in catalog["evals"]:
        pattern = results_pattern(e["results"])
        if pattern and pattern.match(name):
            out.append(e)
    return out
