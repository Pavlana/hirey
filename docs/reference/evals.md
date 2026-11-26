# Evals: getjo's evaluations

[Overview](../how-it-works.md) · [Reference index](README.md)

The Evals page (`/evals`) shows every evaluation of getjo's job scoring in one place: what each one
asks, where and when it runs, its latest result and trend, how to check it and what to change when it
fails. It reads getjo's eval catalogue and results files read only (`prep/evals.py`).

## Contents

- [Sources](#sources)
- [The Evals page](#the-evals-page)
- [Eval cards](#eval-cards)
- [Matching runs to evals](#matching-runs-to-evals)
- [Run details](#run-details)
- [History](#history)
- [The promptfoo report](#the-promptfoo-report)
- [File safety](#file-safety)
- [Empty states](#empty-states)
- [Example data](#example-data)

## Sources

Everything comes from getjo's checkout, the `getjo_repo` setting
([configuration.md](configuration.md)). Nothing there is written, run or imported.

| File | What it holds |
| --- | --- |
| `evals/catalog.json` | One entry per eval: question, dimensions, grader, metrics, where and when it runs, command, results file pattern, steps, code, status |
| `evals/results/runpy-<set>-<YYYYMMDDTHHMMSS>.json` | One `python -m evals.run` run, schema 1 (getjo's `docs/scoring.md`, "Results files") |
| `evals/results/promptfoo-<set>-<YYYYMMDDTHHMMSS>.json` | One promptfoo run in promptfoo's own format |
| `evals/results/promptfoo-<set>-<YYYYMMDDTHHMMSS>.html` | promptfoo's HTML report for the same run |

- `evals.load_catalog` returns a state: `ok`, `no_repo`, `missing` or `invalid`. A status other than
  `built`, `partial` or `gap` is read as `gap`.
- Results files hold case data: job titles, companies, the model's reasons and the labeller's reasons.
  They are shown on the loopback dashboard only. A file that cannot be read is skipped with a warning
  naming the file, never its contents.

## The Evals page

`web.evals_page`, top to bottom:

1. **How evals fit together**: question → cases → grader → metric → decision, and where evals run:
   offline (`evals.run`, promptfoo, by hand), the CI gate (the fictional set on push to main), and
   production (gaps).
2. **Tiles**: evals by status, results files saved, the last run.
3. **Group switch**: `?group=dimension` (default) or `?group=where`. Each eval appears once, under its
   first-listed dimension or place; the rest show as tags. Gaps come last in a group.
4. **Eval cards** per group.

The page has its own nav item between Learn and Metrics and the eucalyptus theme.

## Eval cards

Each card (`web._eval_card`) shows:

- name and status badge (built, partial, gap); a gap card has a dashed border;
- the question, dimension tags, the grader (Code, Your labels, LLM judge), where and when it runs;
- **Last run**: the headline metric of the newest matching results file, its time and a sparkline of
  up to 12 runs, linking to [run details](#run-details) and [history](#history);
- the command with **Copy**;
- **How to check it** and **If it fails**, collapsed;
- **Code**: file · function from the catalogue.

An eval with a results pattern and no runs says "No runs yet" above its command. One without a
pattern says it writes no results file.

### Headline metric

`evals.headline_metric` picks the number a run is summed up by:

1. `agreement` or `verdict`, when the eval lists it;
2. else the first metric the eval lists that the run has;
3. else promptfoo's pass rate.

Values are rates from 0 to 1. A judge metric (such as `rubric_reasons`) is the mean of the judge's
0–1 scores.

## Matching runs to evals

`evals.results_pattern` turns a catalogue `results` pattern into a regular expression on the file
name: `<YYYYMMDDTHHMMSS>` matches a time stamp and `<set>` any set name. A file can belong to several
evals: a promptfoo file matches both its grid eval and the LLM-judge eval. New getjo evals appear on
the page without a change in Hirey.

## Run details

`/evals/run?file=NAME` (`web.eval_run_page`) links back to each eval the file belongs to.

### evals.run results

- Tiles: agreement (with the gate's result and minimum), apply precision, apply recall, personal
  mentions, cost and model.
- **Run**: set, threshold, prompt hash, commit, created time.
- **Confusion matrix**: labels (apply, maybe, skip) against verdicts, plus unscored.
- **Threshold sweep**: precision and recall at thresholds 5–9 (`charts.lines`) and a table; ★ marks
  the threshold in use.
- **Disagreements**: job, expected label, score (and the raw score before a dealbreaker cap), verdict,
  dealbreakers and the first reasons, the label's reason and any personal mentions.

### promptfoo results

`evals.promptfoo_summary` reads `results.prompts[].metrics` per model and `results.results[]` per case
and model. When `prompts` is missing, per-model numbers come from the rows; when
`gradingResult.componentResults` is missing, checks come from `namedScores`.

- Tiles: pass rate, models, cost.
- **Per model**: passed, failed, errors, each named metric's mean, cost. Judge metrics show as a
  0–1 score.
- **Case × model**: per cell PASS or FAIL, the score from `response.output`, each named check (failed
  ones with promptfoo's reason) and the judge's score with its text. The judge has weight 0, so it
  never fails a case.
- **Open promptfoo report**, when the `.html` file exists.

## History

`/evals?eval=ID` shows the eval's card and every run, oldest to newest in a line chart of the headline
metric and newest first in a table: time (links to the run), set, model, prompt hash, commit, value
with the gate's result, cost. A changed prompt hash means the scoring prompt text changed, so a prompt
or model change shows its effect next to the number.

## The promptfoo report

`/evals/report/NAME.html` (`web.eval_report`) serves a saved promptfoo report from `evals/results`,
with its own headers (`web.REPORT_HEADERS`): a `sandbox allow-scripts` CSP with inline styles and
scripts only, no network (`connect-src 'none'`), no fonts, `data:` images only, no framing and
`Referrer-Policy: no-referrer`.

## File safety

`evals.safe_path` is the only way a results file is opened:

- the name must match `^(runpy|promptfoo)-<set>-<YYYYMMDDTHHMMSS>\.(json|html)$`, with `<set>` in
  lowercase letters, digits, `_` and `-`: no folders, no dots, no `%` escapes;
- the resolved path must sit directly in `evals/results`, so a symlink out of it is refused;
- only promptfoo `.html` files are served as reports; files over 20 MB are skipped.

Anything else is a plain 404.

## Empty states

| Case | The page shows |
| --- | --- |
| `getjo_repo` not set | "getjo's checkout is not set" and the setting to add |
| no `evals/catalog.json` | the path expected and a hint to pull getjo's main branch |
| catalogue not valid JSON | the path to check |
| no results files | each card's "No runs yet" with its command |

The header is shown in every case.

## Example data

`python -m prep.seed_example` copies the made-up catalogue and runs in `tests/fixtures/evals/` to
`data/example-getjo-repo/evals/`. Start the dashboard with
`PREP_GETJO_REPO=$PWD/data/example-getjo-repo` to see them ([usage.md](../usage.md#try-it-with-example-data)).
