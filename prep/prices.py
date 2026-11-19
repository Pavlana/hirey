"""Claude API prices in US dollars per million tokens, for the Usage tab's cost estimate.

Source: Anthropic's official pricing page, https://platform.claude.com/docs/en/about-claude/pricing
(table "Model pricing"), read 2026-11-19. These are first-party Claude API list prices at standard
speed and global routing; a Claude subscription is billed differently, so the dashboard calls the
result an estimate. Settings `[prices."<model>"]` override a row (prep/config.py).

Columns: input, output, cache read (hits and refreshes), cache write 5-minute, cache write 1-hour.
A model id not listed here and not in settings has no price: its tokens show as "price unknown",
never at a guessed price.

Modifiers from the same page that apply per response:
- Claude Haiku 5.5 is priced by prompt length: a response whose prompt (input + cache read + cache
  write) is over 100,000 tokens pays the LONG_PROMPT row for every count.
- Fast mode (`usage.speed == "fast"`) has its own input and output prices (FAST); caching
  multipliers apply on top, so cache prices scale by the same factor.
- US-only inference (`usage.inference_geo == "us"`) is 1.1x on every count, Claude 4.6 and later.
"""

Price = tuple[float, float, float, float, float]  # input, output, cache_read, cache_write_5m, cache_write_1h
KINDS = ("input", "output", "cache_read", "cache_write_5m", "cache_write_1h")

DEFAULTS: dict[str, Price] = {
    "claude-fable-5-2":   (8.00, 40.00, 0.20, 10.00, 16.00),
    "claude-mythos-5-2":  (8.00, 40.00, 0.20, 10.00, 16.00),
    "claude-fable-5-1":   (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-mythos-5-1":  (10.00, 50.00, 0.25, 12.50, 20.00),
    "claude-fable-5":     (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-mythos-5":    (10.00, 50.00, 1.00, 12.50, 20.00),
    "claude-opus-5-5":    (4.00, 20.00, 0.20, 5.00, 8.00),
    "claude-opus-5":      (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-8":    (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-7":    (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-6":    (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-5":    (5.00, 25.00, 0.50, 6.25, 10.00),
    "claude-opus-4-1":    (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-opus-4":      (15.00, 75.00, 1.50, 18.75, 30.00),
    "claude-sonnet-5-5":  (2.00, 10.00, 0.10, 2.50, 4.00),
    "claude-sonnet-5":    (2.00, 10.00, 0.20, 2.50, 4.00),
    "claude-sonnet-4-6":  (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4-5":  (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-sonnet-4":    (3.00, 15.00, 0.30, 3.75, 6.00),
    "claude-haiku-5-5":   (0.10, 0.50, 0.01, 0.125, 0.20),
    "claude-haiku-4-5":   (1.00, 5.00, 0.10, 1.25, 2.00),
}

LONG_PROMPT = 100_000
LONG_PROMPT_PRICES: dict[str, Price] = {
    "claude-haiku-5-5": (0.50, 2.50, 0.05, 0.625, 1.00),
}

# Fast mode: (input, output) per million tokens; cache prices scale with input.
FAST: dict[str, tuple[float, float]] = {
    "claude-fable-5-2": (16.00, 80.00),
    "claude-mythos-5-2": (16.00, 80.00),
    "claude-opus-5-5": (8.00, 40.00),
    "claude-opus-5": (10.00, 50.00),
    "claude-opus-4-8": (10.00, 50.00),
}

US_ONLY = 1.1  # earlier models reject inference_geo, so a "us" response is always 4.6 or later


def base_id(model: str | None) -> str | None:
    """The table key for a model id: exact, or with a date suffix (claude-opus-4-1-20250805) or a
    bracketed tag (claude-opus-4-6[1m]). None when it is not a listed model."""
    if not model:
        return None
    m = model.split("[", 1)[0].split("@", 1)[0]
    if m in DEFAULTS:
        return m
    head, _, tail = m.rpartition("-")
    if tail.isdigit() and len(tail) == 8 and head in DEFAULTS:
        return head
    return None
