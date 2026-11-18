Fictional example for `cards import` (format: docs/learn-format.md). Made-up company, made-up
numbers. Try it on the example database:
PREP_DB=data/example.db python -m prep.cli cards import learn.example.md --dry-run

# Search basics
slug: search-basics
subtitle: Retrieval for a made-up help centre
cover: teal

## Retrieval

### What does hybrid search add over vector search alone?
tags: confirmed

- Keyword search finds exact terms: product codes, names, error messages.
- Vector search finds meaning when the words differ.
- Hybrid runs both and merges the two lists (for example Reciprocal Rank Fusion).
- At Initech's help centre: hybrid search fixed questions quoting error codes (4 of 20 → 17 of 20).

follow-ups:
- How are the two result lists merged?
- When would keyword search alone be enough?
sources:
- read | Hybrid search, a short overview | https://example.com/hybrid-search | 5 | how the lists are merged
- notes | Two-minute script: hybrid search

### How do you choose top-K?
tags: accelerator-default, unknown

- Small K: focused and cheap, but the right chunk can be missed.
- Large K: better recall, more noise, cost and latency.
- Measure recall at several K on a labelled set; pick the smallest K that reaches the target.

```python
for k in (3, 5, 10):
    print(k, recall_at_k(questions, k))
```

follow-ups:
- What is recall@K?

# Stories (STAR)
slug: stories
cover: amber

## Story 1: the help-centre bot

### Tell the help-centre bot story in 90 seconds
tags: confirmed

- Context: answers were wrong for error-code questions.
- Action: added keyword search next to vectors; built a 20-question test set.
- Outcome: 4 of 20 → 17 of 20 on the test set.

### Story 1: the numbers
- 20 test questions, labelled by hand.
- 4 correct before, 17 after.
