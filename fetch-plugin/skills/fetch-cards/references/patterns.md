# Card patterns

Copy-pasteable patterns for the Fetch iOS app. Adapt the data to the user's
actual request — these are scaffolds, not templates to fill verbatim.

**Design principle:** cards are typographic. No icon chips, no decorative
symbols, no heavy CTA buttons. The title commands the card; stats and item
rows carry the data through type hierarchy alone. This is the ChatGPT/Claude
brand-card register — minimal, editorial, restrained.

Omit `symbol` from every field unless you have a specific, semantic reason
not to (you almost never do).

---

## 1. Daily brief

The workhorse. Title, two big-number stats, a clean list of today's events,
and a footer with the update time. No icons.

```card
{
  "title": "Today",
  "stats": [
    { "label": "Meetings", "value": 3 },
    { "label": "Tasks", "value": 12 }
  ],
  "items": [
    { "title": "Standup", "subtitle": "9:30 — Zoom", "url": "https://zoom.us/j/123" },
    { "title": "1:1 with Sam", "subtitle": "11:00 — Meet", "url": "https://meet.google.com/abc" },
    { "title": "Design review", "subtitle": "2:00 — Office" }
  ],
  "footer": "Updated 9:12 AM"
}
```

## 2. Metrics dashboard

Four short stats render as a clean big-number row. Use this for a quick
numerical snapshot — PRs, uptime, revenue, etc.

```card
{
  "title": "Repo health",
  "stats": [
    { "label": "Open PRs", "value": 7 },
    { "label": "Merged (7d)", "value": 23 },
    { "label": "Issues", "value": 4 },
    { "label": "Stars", "value": 1842 }
  ],
  "footer": "hermes-agent/main · last 7 days"
}
```

When a value is too long for a big number (e.g. "98.7%" is fine, "98.74% and holding" is not), the renderer auto-switches to key-value rows:

```card
{
  "title": "Service status",
  "stats": [
    { "label": "Uptime", "value": "99.97%" },
    { "label": "p50 latency", "value": "142ms" },
    { "label": "Region", "value": "us-west-2 (Oregon)" },
    { "label": "Last incident", "value": "14 days ago" }
  ],
  "footer": "Healthy · no active incidents"
}
```

Notice the long "us-west-2 (Oregon)" value shifts the whole stat block into
key-value mode. If you want some stats as columns and some as rows, split
into two cards.

## 3. Link carousel

The "hotels in Paris" layout. Use `cards` (plural) to render a horizontal
scrolling row of sub-cards, each with an image, badge, title, and
description. The entire sub-card is the tap target — **no `cta` field**,
no button. The press feedback (scale + dim) signals tappability.

```card
{
  "title": "Cafés near you",
  "subtitle": "Open now · within 1 km",
  "cards": [
    {
      "title": "Blue Bottle Coffee",
      "subtitle": "Single-origin pour-overs. Quiet, lots of outlets.",
      "image": "https://example.com/blue-bottle.jpg",
      "badge": "4.6 ★",
      "url": "https://maps.apple.com/?q=Blue+Bottle+Coffee"
    },
    {
      "title": "Sightglass Coffee",
      "subtitle": "Industrial space, espresso bar downstairs.",
      "image": "https://example.com/sightglass.jpg",
      "badge": "4.5 ★",
      "url": "https://maps.apple.com/?q=Sightglass+Coffee"
    },
    {
      "title": "Ritual Coffee",
      "subtitle": "Cult favorite. Small, lively.",
      "image": "https://example.com/ritual.jpg",
      "badge": "4.4 ★",
      "url": "https://maps.apple.com/?q=Ritual+Coffee"
    }
  ],
  "footer": "3 spots · tap a card for directions"
}
```

**Carousel gotcha:** each sub-card is 246pt wide. Three or four sub-cards is
the sweet spot on a phone; beyond five the carousel scrolls but most users
won't scroll past the first screen. Prefer quality over quantity.

## 4. Search results

A tappable list of results. Status lives in the trailing `value` text, not
in icons. Good for "show me the top N matching things."

```card
{
  "title": "Matching PRs",
  "items": [
    { "title": "Fix flaky test in session_compression", "subtitle": "alice/hotfix-flaky-test · 2 checks passing", "value": "ready", "url": "https://github.com/hermes-agent/hermes-agent/pull/412" },
    { "title": "Add Langfuse v3 support", "subtitle": "bob/langfuse-v3 · 1 check failing", "value": "blocked", "url": "https://github.com/hermes-agent/hermes-agent/pull/409" },
    { "title": "Refactor prompt builder tiers", "subtitle": "carol/prompt-builder-tiers · draft", "value": "draft", "url": "https://github.com/hermes-agent/hermes-agent/pull/401" }
  ],
  "footer": "3 of 12 results · say \"show all\" for the rest"
}
```

## 5. Status summary

One card, one decision: is the thing healthy or not? The status word
("Healthy" / "Blocked") is a stat value. The footer says what to do next.

```card
{
  "title": "Deploy check",
  "stats": [
    { "label": "Status", "value": "Healthy" },
    { "label": "Checks", "value": "12 / 12" }
  ],
  "items": [
    { "title": "Lint", "subtitle": "ruff · 0 errors", "value": "pass" },
    { "title": "Type check", "subtitle": "mypy · 0 errors", "value": "pass" },
    { "title": "Tests", "subtitle": "pytest · 847 passed", "value": "pass" },
    { "title": "Build", "subtitle": "docker · 2m 14s", "value": "pass" }
  ],
  "footer": "Ready to merge · main is green"
}
```

When something is wrong, swap the status word and the footer:

```card
{
  "title": "Deploy check",
  "stats": [
    { "label": "Status", "value": "Blocked" },
    { "label": "Checks", "value": "11 / 12" }
  ],
  "items": [
    { "title": "Lint", "subtitle": "ruff · 0 errors", "value": "pass" },
    { "title": "Type check", "subtitle": "mypy · 4 errors", "value": "fail" },
    { "title": "Tests", "subtitle": "pytest · 846 passed, 1 failed", "value": "1 fail" }
  ],
  "footer": "Fix mypy errors · see logs for details"
}
```

## 6. Weekly review

A summary of the week with a pair of stats and a short list of highlights.
Use this for end-of-week or end-of-sprint summaries.

```card
{
  "title": "Week of Jun 17",
  "stats": [
    { "label": "Shipped", "value": 14 },
    { "label": "Still open", "value": 6 }
  ],
  "items": [
    { "title": "Fetch card generation shipped", "subtitle": "platform_hint + skill + test", "url": "https://github.com/hermes-agent/hermes-agent/pull/415" },
    { "title": "Migrated prompt builder to tiers", "subtitle": "stable → context → volatile", "url": "https://github.com/hermes-agent/hermes-agent/pull/401" },
    { "title": "Fixed cron double-fire", "subtitle": "race in scheduler poll loop", "url": "https://github.com/hermes-agent/hermes-agent/pull/422" }
  ],
  "footer": "14 merged, 6 carried to next week"
}
```

---

## Tips

- **Title is the cheapest useful card.** If you only have a label and a vibe,
  a title-only card with stats or a footer is better than padding with empty
  sections.
- **Stats are more readable than tables for 2-4 numbers.** Use a stat row;
  reserve tables for larger matrices.
- **Carousel for browsing, list for choosing.** If the user is picking one
  thing from many, use `items` (a list). If the user is browsing options,
  use `cards` (a carousel). Never use `cta` on carousel sub-cards — the
  whole card is the tap target.
- **Footer carries the "so what."** "3 of 12 results · say 'show all'", "Ready
  to merge", "Updated 9:12 AM" — the footer is where the user learns what to
  do next.
- **Don't repeat the card in prose.** If the card says "3 meetings today",
  don't open the reply with "You have 3 meetings today." Let the card carry
  the data; let prose carry the framing or transition between cards.
