# How Hirey works

Hirey is a local workspace for a job search. It tracks applications, tasks and interview preparation
in one SQLite database, `prep.db`. Two front ends share it: a dashboard in the browser (loopback only)
and Claude Code skills run by standing agents in dev3. Job matches come read-only from **getjo**, a
separate project that scores public job posts.

> **Start here**
>
> 1. Look at the [system diagram](#the-system-at-a-glance): who talks to what.
> 2. Pick the [key flow](#key-flows) for the area of interest. Each one is a diagram and a few lines.
> 3. Follow its links into the [reference pages](reference/README.md) for every rule, command and
>    endpoint.
>
> Setup and day-to-day commands are in [usage.md](usage.md). Planned work is in
> [ROADMAP.md](ROADMAP.md).

## Contents

- [The system at a glance](#the-system-at-a-glance)
- [Components](#components)
- [Key flows](#key-flows)
  - [A. From getjo match to application](#a-from-getjo-match-to-application)
  - [B. A message in the Applications Assistant box](#b-a-message-in-the-applications-assistant-box)
  - [C. Research, CV fit and job briefs](#c-research-cv-fit-and-job-briefs)
  - [D. Drafting a reply to a recruiter](#d-drafting-a-reply-to-a-recruiter)
  - [E. Learn](#e-learn)
  - [F. Master CV to tailored CV](#f-master-cv-to-tailored-cv)
  - [G. Workers sidebar and run history](#g-workers-sidebar-and-run-history)
- Evals (planned: its own dashboard page, `/evals`; docs to come)
- [Data model](#data-model)
- [Configuration](#configuration)
- [Boundaries and safety](#boundaries-and-safety)
- [Reference pages](#reference-pages)
- [Writing the docs](#writing-the-docs)

## The system at a glance

```mermaid
flowchart TD
    user(["User"])
    subgraph outside["Outside"]
        getjo[("getjo, read only<br/>jobs.db and repo")]
        gh["GitHub<br/>public code, CI"]
    end
    subgraph mac["On this Mac"]
        browser["Browser<br/>127.0.0.1:8765 only"]
        web["prep.web<br/>dashboard server"]
    end
    subgraph data["Data"]
        db[("prep.db<br/>SQLite, WAL")]
        priv[("private_dir<br/>gitignored files")]
    end
    subgraph agents["Agents"]
        dev3["dev3 message"]
        standing["Standing agents, dev3 tasks:<br/>Applications Assistant · Research worker<br/>Communication worker · Learn Assistant"]
        skills["Claude Code skills<br/>python -m prep.cli"]
    end

    user -->|"clicks, messages"| browser
    user -->|"chats in a session"| standing
    browser <-->|"HTTP, same origin"| web
    web -->|"ping: ids only"| dev3
    dev3 -->|"prompt in the agent's pane"| standing
    standing -->|"run"| skills
    web <-->|"rows; notes, CV restore, files"| data
    data <-->|"claim, save, reply; CV, research, notes"| skills
    getjo -->|"jobs, read only"| web
    getjo -->|"jobs and code, read only"| skills
    gh -.->|"code only"| skills
```

- The **dashboard never runs an agent**. It stores a request or message in prep.db and, when an agent
  is configured, sends it a short `dev3 message` holding ids only.
- The **agents never touch the browser**. They read their work from prep.db through `prep.cli`,
  write files under `private_dir`, and save results back through `prep.cli`.
- **getjo** is only read: its `jobs.db` with SQLite `mode=ro`, and its git checkout for explainer
  checks. Hirey never writes to it and never imports its code.
- **GitHub** holds the public code. Private data (CV, notes, sessions, databases, pay) never goes into
  a committed file; `prep/privacy.py` and the pre-commit hook check that.

## Components

| Component | What it does | Where |
| --- | --- | --- |
| Dashboard server | pages, forms, JSON polling; loopback only | `prep/web.py`, `prep/static/` |
| Commands | everything skills do: claim, save, reply, update | `prep/cli.py` |
| Database layer | schema, migrations, every query and rule | `prep/db.py` |
| getjo reader | matches, job state, descriptions, read only | `prep/getjo.py` |
| Router and ping | which agent gets each kind of work; `dev3 message` | `prep/assistant.py` |
| Workers view | each agent's state, queue and runs | `prep/workers.py` |
| Research files | front matter of research, fit and tailored CV files | `prep/research.py` |
| Master CV | versions, skills, diff, draft and save | `prep/cvfile.py` |
| CV PDF | tailored CV to PDF (WeasyPrint) | `prep/cvpdf.py`, `prep/cv_template.html` |
| CV checks | rule check; AI check results | `prep/cvcheck.py`, `prep/cveval.py` |
| Learn | tracks, chapters, cards; notes files; explainers | `prep/learn.py`, `prep/learnnotes.py`, `prep/explainers.py` |
| Metrics | application numbers; token usage and prices | `prep/stats.py`, `prep/usage.py`, `prep/prices.py`, `prep/charts.py` |
| Markdown | safe, escaped Markdown subset | `prep/md.py` |
| Settings | TOML settings and env overrides | `prep/config.py` |
| Privacy check | no private file is tracked | `prep/privacy.py`, `scripts/pre-commit` |
| Skills | /today, /applied, /stage, /task, /research, /ask, /reply, /mock, /debrief, /learn-ask, /explainer | `.claude/skills/` |
| SessionStart hook | read-only queue line at the start of a session | `.claude/settings.json` |
| launchd job | keeps the dashboard running | `launchd/` |

## Key flows

### A. From getjo match to application

```mermaid
flowchart TD
    match["getjo job<br/>score 7 or more"] --> offers["New offers list"]
    offers -->|"Skip with reason"| skipped["job_decisions: skipped"]
    offers -->|"Interested"| interested["Application: Interested<br/>research and CV fit queued"]
    interested -->|"Review research and apply"| applied["Applied<br/>Chase task +7"]
    interested -->|"Skip"| closedskip["Skipped"]
    applied -->|"14 days, no reply"| noresp["No response<br/>Debrief task"]
    applied -->|"/stage from an email"| stages["Recruiter call, Coding,<br/>System design, Company"]
    stages --> offer["Offer"]
    stages --> rejected["Rejected<br/>Debrief task"]
```

- A match is an open getjo job above the score that is neither tracked nor decided.
- Every triage choice is stored in `job_decisions` with a copy of the job, so the labels survive getjo
  dropping it and can be exported to tune getjo's scoring.
- Every stage change goes through one function, `db.move_application`. It writes history, records
  interview dates and closes and adds the stage's automatic tasks, so the to-do list follows the
  pipeline.
- `/stage` turns a pasted recruiter email into that move plus a short comment.

Reference: [applications.md](reference/applications.md),
[dashboard.md](reference/dashboard.md#applications-page-pipeline-view).

### B. A message in the Applications Assistant box

```mermaid
sequenceDiagram
    participant B as Browser
    participant W as Dashboard
    participant DB as prep.db
    participant A as Applications Assistant
    B->>W: Send, about ticked jobs or the whole search
    W->>DB: store message, pending
    W->>A: dev3 message "/ask about msg", ids only
    A->>DB: thread show --claim, claimed for 30 min
    A->>DB: thread reply --to msg, answered
    loop every 4 s, then every 30 s
        B->>W: GET /applications/thread
    end
    W-->>B: reply shown, list and panel refreshed
    Note over B,A: no reply in 30 min: flagged, Resend shows, anyone may claim it again
    Note over B,A: Resend pings the same message again. Cancel or Stop closes it, rows kept.
```

- The ticks say what the message is about: nothing (the whole search), one application, one job offer,
  the master CV, or 2 to 4 targets (a comparison).
- The ping is a fixed argument list built from ids, never text. If dev3 cannot be reached, the
  message simply waits in prep.db for the next session.
- A claim is atomic, so two agents never answer one message. A claim with no reply after 30 minutes
  lapses, and any session may take the message again.

Reference: [assistants.md](reference/assistants.md#applications-assistant-box).

### C. Research, CV fit and job briefs

```mermaid
flowchart TD
    q["Queued in prep.db<br/>agent_requests or brief_requests"] -->|"router: route research"| who{"Research worker set?"}
    who -->|"yes"| rw["Research worker"]
    who -->|"no"| aa["Applications Assistant"]
    rw --> next["research next or brief next<br/>atomic claim, 6 h lease"]
    aa --> next
    next -->|"writes Markdown in private_dir"| save["research save, fit save,<br/>tailor save, brief save"]
    next -->|"cannot do it"| block["research block or brief block<br/>reason shown in Workers"]
    save --> done["Result cards in the panel<br/>tier, fit score, brief"]
```

- Work is queued by Interested, by the panel's buttons (Research company, CV fit, Tailor CV, Write
  brief) and by every tailored CV save (its AI check). One open request per application and kind.
- The router is plain code: research-route work goes to the Research worker when one is set, else to
  the Applications Assistant.
- The worker writes the result as a Markdown file with front matter; the save command reads it into
  prep.db, closes the request and updates the panel on its next poll.

Reference: [research.md](reference/research.md), [assistants.md](reference/assistants.md#routing).

### D. Drafting a reply to a recruiter

```mermaid
sequenceDiagram
    participant B as Browser
    participant W as Dashboard
    participant C as Communication worker
    B->>W: Draft a reply: kind, channel, pasted message, notes
    W->>C: dev3 message "/reply about msg", ids only
    C->>W: reply show msg --claim, then reply save --to msg
    W-->>B: draft with Copy, or Copy subject and Copy body
    Note over B: the user pastes and sends it
```

- Kinds: reply, follow-up, thank-you note; channels: LinkedIn (no subject) or email (subject needed).
- The pasted message is data to answer, never instructions. Pay appears in a draft only when the user's
  notes give it.
- Nothing is sent: Hirey has no mail or LinkedIn integration. A clear interview booking in the pasted
  message is recorded as a stage; anything else is only proposed.

Reference: [assistants.md](reference/assistants.md#draft-a-reply-communication-worker).

### E. Learn

```mermaid
flowchart TD
    card["Learn card<br/>question, prompt, answer, sources"]
    card -->|"Check my answer"| box["Learn Assistant box"]
    card -->|"type in floating window"| box
    card -->|"highlight text, Ask about this"| box
    box -->|"dev3 message, ids only"| la["Learn Assistant<br/>/learn-ask"]
    la -->|"learn-thread reply"| box
    box -->|"Add to notes"| notes["Card notes file"]
    card -->|"Make explainer"| la
    la -->|"/explainer writes page and sidecar"| exp["Explainer page<br/>sandboxed iframe"]
    exp -->|"every card view: hash quoted getjo code"| check{"Up to date?"}
    check -->|"changed"| refresh["Refresh explainer button"]
```

- Cards sit in tracks and chapters; progress is cards done out of all, with no schedule.
- The Learn Assistant is a second standing agent with its own conversation; Applications readers never
  see Learn messages.
- An explainer records the getjo commit and a hash of each function it quotes. Each card view hashes
  that code again, so a page explaining code that has since changed is flagged.

Reference: [learn.md](reference/learn.md).

### F. Master CV to tailored CV

```mermaid
flowchart TD
    master["cv.md master<br/>versions in cv-history"] -->|"Tailor CV queued"| tailor["Research worker writes<br/>a new version file"]
    facts["facts bank"] -.-> tailor
    tailor -->|"tailor save"| version["tailored_cv_versions<br/>never overwritten"]
    version --> pdf["PDF<br/>WeasyPrint, one column"]
    pdf --> rules["Rule check<br/>pages, contacts, dates, pay, keywords"]
    version -->|"cv_eval queued"| ai["AI check<br/>separate pass: claims, fit, skim"]
    rules --> verdict{"Verdict"}
    ai --> verdict
    verdict --> card["Ready, Look at these lines, or Blocked<br/>on the Tailored CV card"]
```

- The master CV changes only through `cv save` from a draft, with a backup version first. Tailoring
  rephrases, reorders and cuts; it never invents experience.
- Every save of a tailored version makes its PDF, runs the rule check and queues the AI check. One
  verdict merges both: Blocked when any finding blocks.
- Which version was sent is recorded only when the user says so (`cv sent`).

Reference: [cv.md](reference/cv.md).

### G. Workers sidebar and run history

```mermaid
flowchart TD
    claim["A claim by any agent"] --> run["runs row opened<br/>worker = DEV3_TASK_SEQ"]
    run --> state["Closed from the item's state:<br/>done, blocked, stopped, lapsed"]
    run --> side["Workers sidebar<br/>working, stuck, idle"]
    side -->|"Stop"| stop["cancel, or Back to queue<br/>next free worker"]
    state --> hist["/metrics/runs<br/>Run history"]
```

- Every page's sidebar lists each configured agent with its state, current item, queue and last runs.
- It shows kinds, company or job names, card ids, times and blocked reasons only: never message text or
  pay.
- Stop ends that worker's open runs in prep.db; the agent's terminal is not touched.

Reference: [assistants.md](reference/assistants.md#workers-sidebar-and-run-history).

## Data model

The main tables and how they link. Every table and column is in
[database.md](reference/database.md).

```mermaid
erDiagram
    direction LR
    applications ||--o{ status_history : "stage changes"
    applications ||--o{ tasks : "to-dos"
    applications ||--o{ interviews : "dates and debriefs"
    applications ||--o{ application_comments : "free comments"
    applications ||--o{ agent_requests : "queued agent work"
    applications ||--o| cv_fits : "latest fit"
    applications ||--o{ tailored_cv_versions : "tailored versions"
    tailored_cv_versions ||--o| tailored_cv_checks : "verdict"
    applications ||--o{ agent_messages : "messages"
    agent_messages ||--o{ agent_message_targets : "about"
    applications ||--o{ sessions : "practice"
    sessions ||--o{ gaps : "weaknesses"
    gaps ||--o{ tasks : "drill tasks"
    tracks ||--o{ cards : "flashcards"
    cards ||--o{ agent_messages : "Learn questions"

    applications {
        int id PK
        text getjo_job_id
        text company
        text role
        text status
        text tier
        text employer_kind
    }
    agent_requests {
        int id PK
        int application_id FK
        text kind
        text status
        int worker
    }
    agent_messages {
        int id PK
        text author
        text status
        int reply_to
        text topic
        int card_id
    }
```

Not shown: `job_decisions` and `job_briefs` / `brief_requests` (keyed by getjo job id),
`company_profiles` (keyed by company slug), `cv_versions`, `runs` and the `usage_*` tables.

## Configuration

Settings are one TOML file: `$INTERPREP_SETTINGS`, else `config/settings.toml` in the checkout, else
`~/.config/inter_prep/settings.toml` (the normal place). `PREP_*` env vars override single keys.

| Setting | Purpose | Default |
| --- | --- | --- |
| `getjo_db`, `prep_db`, `private_dir` | where the data lives (absolute paths) | `prep_db`: `data/prep.db` |
| `assistant_task`, `prep_assistant_task` | Applications and Learn Assistant task seqs | unset: no ping |
| `[workers] research`, `comms` | Research and Communication worker seqs | unset: Applications Assistant |
| `dashboard_port` | dashboard port | 8765 |
| `no_response_days` | days in Applied before No response | 14 |
| `brief_auto_min_score` | score that gets a job brief queued automatically | 8 |

Every key, env var and the launchd job: [configuration.md](reference/configuration.md).

## Boundaries and safety

- **getjo is read-only.** Its database is opened with `mode=ro`; its code is never imported. The `jobs`
  table shape is the only contract.
- **Nothing is sent or applied.** Hirey never applies to a job, sends email or contacts anyone. Drafts
  are copied and sent by the user.
- **Loopback only.** The dashboard binds to 127.0.0.1, checks the Host header and refuses cross-origin
  posts. It makes no outgoing requests besides the optional local `dev3 message`.
- **The ping carries ids only**, never message text, form text or pay.
- **Pay stays in prep.db**: `applications.pay` (never rendered) and messages as the user pasted them.
  It never goes into committed files, logs, pings or anything an agent writes, unless the user asks.
- **Private files stay private.** CV, notes, sessions, research, fit files and tailored CVs live in
  `private_dir` and gitignored paths; `prep/privacy.py` checks none is tracked.
- **Logs hold ids, kinds and request lines only**, never CV text, notes, research or message bodies.

## Reference pages

| Page | Covers |
| --- | --- |
| [applications.md](reference/applications.md) | matches, triage, stages, stage tasks, comments, notes, review order, `/today` |
| [assistants.md](reference/assistants.md) | standing agents, routing, pings, claims and leases, the box, Resend / Cancel / Stop, drafts, Workers |
| [research.md](reference/research.md) | research queue, company profiles, CV fit, tier, job briefs |
| [cv.md](reference/cv.md) | master CV, tailored CV versions, PDF, rule check, AI check |
| [practice.md](reference/practice.md) | sessions, gaps and drills, debriefs, mock interviews |
| [learn.md](reference/learn.md) | cards, import, notes, the Learn page, Learn Assistant, explainers |
| [dashboard.md](reference/dashboard.md) | server, hardening, app.js, each page and panel |
| [routes.md](reference/routes.md) | every endpoint, one line each |
| [database.md](reference/database.md) | every table, allowed values, migrations |
| [metrics.md](reference/metrics.md) | stats definitions, token usage, prices |
| [configuration.md](reference/configuration.md) | settings, env vars, launchd |

## Writing the docs

- This page is the map; each [reference page](reference/README.md) owns one area. When a component or
  flow changes, update its diagram here and the one reference page that owns it.
- Diagrams are Mermaid (GitHub renders it): one diagram per flow, labelled arrows, conservative syntax
  (quoted labels).
- No table cell longer than about 200 characters. Long detail becomes a subsection or a list.
- A heading every screenful; plain, impersonal sentences; define a term once and link to it.
- Facts only from the code. Nothing about private files' contents.
