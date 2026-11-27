# Routes

[Overview](../how-it-works.md) · [Reference index](README.md)

Every dashboard endpoint, one line each. Details live on the page linked in the last column.

## Contents

- [Rules for every request](#rules-for-every-request)
- [Pages](#pages)
- [JSON and file endpoints](#json-and-file-endpoints)
- [Applications and tasks](#applications-and-tasks)
- [New offers](#new-offers)
- [Applications Assistant box](#applications-assistant-box)
- [CV and workers](#cv-and-workers)
- [Learn](#learn)
- [Redirects from old URLs](#redirects-from-old-urls)

## Rules for every request

- Host must be `localhost` or `127.0.0.1` (any port), else 400.
- POST: a cross-origin `Origin` (including `null`) or `Sec-Fetch-Site` is a 403. Every POST action is in
  `web.ACTIONS`.
- POST with `Accept: application/json` answers JSON (`ok`, `message`, often `html`, `redirect`); a
  plain form post redirects (303) to its `return` URL with a fixed message key. Bad input is a 400.
- Other methods are a 405; an unknown path is a 404.
- See [dashboard.md](dashboard.md#local-hardening).

## Pages

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| GET | `/` | Applications page: Pipeline or New offers view, panels, My CV | [dashboard.md](dashboard.md) |
| GET | `/stats` | Pipeline stats | [metrics.md](metrics.md#pipeline-stats-page) |
| GET | `/evals` | Evals: eval map (`group=dimension\|where`), or one eval's history (`eval=ID`) | [evals.md](evals.md) |
| GET | `/evals/run?file=NAME` | one results file in detail | [evals.md](evals.md#run-details) |
| GET | `/evals/report/NAME.html` | a saved promptfoo report, sandboxed, own headers | [evals.md](evals.md#the-promptfoo-report) |
| GET | `/runs` | Run history, filters by worker and result | [assistants.md](assistants.md#workers-sidebar-and-run-history) |
| GET | `/learn` | Learn: tracks grid, track, card view | [learn.md](learn.md) |
| GET | `/research/file?app=ID&kind=K[&v=N]` | a company profile, CV fit file or tailored CV, read-only | [research.md](research.md) |
| GET | `/offers/skip?job_id=…` | no-script Skip form for an offer | [dashboard.md](dashboard.md#forms-without-javascript) |
| GET | `/applications/skip?app=ID` | no-script Skip form for an application | [dashboard.md](dashboard.md#forms-without-javascript) |
| GET | `/static/…` | `style.css`, `app.js`, `vim.js`, `explainer.css`, `explainer.js` | [dashboard.md](dashboard.md#look-and-themes) |

## JSON and file endpoints

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| GET | `/applications/thread[?app=ID\|?job=ID]` | poll: `{ok, pending, status, messages, html}` | [dashboard.md](dashboard.md#polling) |
| GET | `/workers.json` | Workers sidebar state and finished long items | [assistants.md](assistants.md#workers-sidebar-and-run-history) |
| GET | `/research/pdf?app=ID&v=N` | a tailored CV version's PDF, inline, own headers | [cvs.md](cvs.md) |
| GET | `/learn/thread?card=ID` | Learn box poll: pending, count, html, explainer state | [learn.md](learn.md) |
| GET | `/learn/answer?msg=ID` | highlight box poll: one question's answer | [learn.md](learn.md) |
| GET | `/learn/notes?card=ID[&note=all]` | a Learn note's text and stamp | [learn.md](learn.md) |
| GET | `/learn/explainer?card=ID` | a card's explainer page, sandboxed, own headers | [learn.md](learn.md) |

## Applications and tasks

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| POST | `/applications/apply` | Mark applied (today) | [applications.md](applications.md#mark-applied-and-skip) |
| POST | `/applications/unapply` | Undo Mark applied, right after only | [applications.md](applications.md#mark-applied-and-skip) |
| POST | `/applications/skip` | Skip an Interested application with a reason | [applications.md](applications.md#mark-applied-and-skip) |
| POST | `/applications/research` | queue research, CV fit or tailored CV (Redo sets `force`) | [research.md](research.md) |
| POST | `/applications/notes` | save the Notes card (`app_id`, `stamp`, `notes`); stale stamp is a 400 | [applications.md](applications.md#notes) |
| POST | `/applications/employer` | set `employer_kind`: product, client or empty for unknown | [dashboard.md](dashboard.md#the-application-panel) |
| POST | `/tasks/done` | tick a panel task done | [applications.md](applications.md#tasks) |
| POST | `/tasks/reopen` | Undo a ticked task | [applications.md](applications.md#tasks) |

## New offers

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| POST | `/offers/interested` | Interested: creates the `to_apply` application, queues research | [applications.md](applications.md#recording-an-application) |
| POST | `/offers/uninterested` | Undo Interested, while nothing else happened | [applications.md](applications.md#new-offers-triage) |
| POST | `/offers/skip` | skip an offer with a reason and comment | [applications.md](applications.md#new-offers-triage) |
| POST | `/offers/unskip` | Undo skip | [applications.md](applications.md#new-offers-triage) |
| POST | `/offers/brief` | Write brief / Redo brief (offers and applications) | [research.md](research.md) |
| POST | `/offers/interviews` | Held: a company's interview style, yes (ranked) or no (filtered out) | [search.md](search.md#company-checks) |
| POST | `/offers/apply`, `/matches/apply` | old form actions, no button; record as applied | [applications.md](applications.md#recording-an-application) |

## Applications Assistant box

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| POST | `/applications/message` | store a message (`about` targets) and ping `/ask` | [assistants.md](assistants.md#sending-a-message) |
| POST | `/applications/draft` | Draft a reply request, pinged to the comms route | [assistants.md](assistants.md#draft-a-reply-communication-worker) |
| POST | `/applications/invite` | Paste recruiter message (`pasted`): an invitation request, pinged to the comms route | [assistants.md](assistants.md#invitations) |
| POST | `/applications/resend` | put a waiting message back to pending and ping again | [assistants.md](assistants.md#resend-and-cancel) |
| POST | `/applications/cancel` | cancel one waiting message | [assistants.md](assistants.md#resend-and-cancel) |
| POST | `/applications/stop` | cancel every waiting message and the open panel's work | [assistants.md](assistants.md#stop-and-no-reply) |

## CV and workers

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| POST | `/cv/restore` | restore a master CV version (`version`; JSON `{version, previous}`) | [cvs.md](cvs.md) |
| POST | `/cv/edit` | save the My CV editor (`stamp`, `text`, `note`); a stale stamp is a 400 with the editor; JSON `{version, previous}` | [cvs.md](cvs.md#editor) |
| POST | `/applications/plan` | save the Tailoring plan card's editor (`app_id`, `stamp`, `text`); a stale stamp is a 400 | [cvs.md](cvs.md#tailoring-plan) |
| POST | `/applications/plan/write` | Write the CV: the plan is agreed and a `tailor` request queued (saves `text` first when sent) | [cvs.md](cvs.md#tailoring-plan) |
| POST | `/applications/cv/edit` | save the Tailored CV editor over the latest version (`app_id`, `version`, `stamp`, `text`) | [cvs.md](cvs.md#editor) |
| POST | `/applications/cv/undo` | bring back the latest version's text from before its newest edit (`app_id`) | [cvs.md](cvs.md#editor) |
| POST | `/applications/cv/revise` | ask the Applications Assistant to revise only the commented lines (`app_id`, or `topic=cv`) | [cvs.md](cvs.md#comments-for-the-agent) |
| POST | `/workers/stop` | end one worker's open runs, optionally back to the queue | [assistants.md](assistants.md#workers-sidebar-and-run-history) |

## Learn

| Method | Path | What it does | Reference |
| --- | --- | --- | --- |
| POST | `/learn/done` | mark a card done, go to the next to do | [learn.md](learn.md) |
| POST | `/learn/undo` | put a card back to do | [learn.md](learn.md) |
| POST | `/learn/source` | tick or untick one Learn source | [learn.md](learn.md) |
| POST | `/learn/notes` | save a card note, all-prep note or Your answer (stamped) | [learn.md](learn.md) |
| POST | `/learn/message` | store a Learn Assistant message (optional quote) and ping | [learn.md](learn.md) |
| POST | `/learn/check` | Check my answer | [learn.md](learn.md) |
| POST | `/learn/card` | Want a card for this? | [learn.md](learn.md) |
| POST | `/learn/add-note` | add a reply's essence to the card's notes | [learn.md](learn.md) |
| POST | `/learn/quote-note` | add a highlighted passage to the notes | [learn.md](learn.md) |
| POST | `/learn/explainer/make` | ask for an explainer | [learn.md](learn.md) |
| POST | `/learn/explainer/refresh` | ask to refresh a changed explainer | [learn.md](learn.md) |
| POST | `/learn/resend` | Resend a waiting Learn message | [assistants.md](assistants.md#resend-and-cancel) |
| POST | `/learn/cancel` | cancel a waiting Learn message | [assistants.md](assistants.md#resend-and-cancel) |
| POST | `/learn/stop` | cancel every waiting Learn message | [learn.md](learn.md) |

## Redirects from old URLs

| Old path | Goes to |
| --- | --- |
| `/dashboard`, `/progress`, `/metrics?tab=applications` | `/stats` |
| `/metrics`, `/usage` | `/stats` |
| `/metrics/runs` | `/runs` with the same query |
| `/applications`, `/tasks` | `/` with the same query |
| `/offers`, `/matches` | `/?view=offers` with `score` and `loc` kept, `company`/`role` → `q`, `show=skipped` → `skipped=1` |
| `/offers?show=interested\|applied` | `/?stage=interested\|applied` (the pipeline) |
