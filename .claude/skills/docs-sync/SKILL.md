---
name: docs-sync
description: Bring the docs in step with a code change before opening a pull request in Hirey — find the doc pages that own the changed code (docs/docs-map.json), update them and their diagrams in the house style, retake affected screenshots on made-up data, and run the docs check until it passes. Use before `dev3 pr create` on any change to prep/, .claude/skills/, scripts/ or config, when the docs check (CI or `python -m prep.docs_check`) fails, or when the user types /docs-sync.
---

# /docs-sync

Run every command from the repo root. The check is `prep/docs_check.py`; GitHub runs the same check
on every pull request (`.github/workflows/docs.yml`).

## 1. See what the change touches

```
python -m prep.docs_check --changed origin/main
```

It lists, with the page to fix:
- code facts missing from their reference page (a new route, setting, table or column, CLI command,
  skill);
- broken links or anchors, missing images, screenshots no doc shows;
- changed code whose owning page (from `docs/docs-map.json`) did not change;
- changed code no rule in the map covers.

## 2. Update the owning pages

- Read `git diff origin/main...HEAD` for the areas listed. Update the page the map names, and the
  flow diagram in `docs/how-it-works.md` when a flow or component changed.
- Follow "Writing the docs" in CLAUDE.md and docs/reference/README.md: one fact on one page, links
  elsewhere, Mermaid diagrams, short table cells, impersonal sentences, nothing about private files.
- Describe what the code does now (read it); never invent behaviour.
- New code file with no rule: add a rule to `docs/docs-map.json` naming the page that owns it.

## 3. Screenshots, when the UI changed

```
python scripts/screenshots.py [NAME ...]
```

It retakes page-level shots on example data (empty settings, no assistants or workers, made-up
companies) and lists the shots that need a click first. Retake those by hand with the same setup
(docs/usage.md, "Try it with example data"). Check every image shows only fictional data.

## 4. Check again, then open the PR

```
python -m prep.docs_check --changed origin/main
python -m unittest
```

Both must pass. If a change really needs no doc change (a refactor, tests only, a typo), say so in
the pull request description on its own line, and CI accepts it:

```
Docs: not needed, <reason>
```

Never use that line to skip a page that describes behaviour you changed.
