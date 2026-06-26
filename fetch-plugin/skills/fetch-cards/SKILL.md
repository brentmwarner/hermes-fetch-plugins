---
name: fetch-cards
description: "Emit generative-UI `card` fences for the Fetch iOS app. Patterns for daily briefs, native chart cards, metrics dashboards, link carousels, and summaries. Use when the user's reply would be clearer as a structured card than as prose and the session is on a Fetch channel."
version: 1.2.0
metadata:
  hermes:
    tags: [fetch, generative-ui, cards, ios, mobile]
    related_skills: []
---

# fetch-cards: Generative-UI cards for the Fetch iOS app

## When to use this skill

Use this skill when the session is on a **Fetch channel** (the iOS app) and
the user's reply would be clearer as a structured card than as prose.

The Fetch app's markdown renderer detects fenced code blocks with the
language `card` (also accepts `cards`, `hermes-card`, `ui`), parses the JSON
inside, and renders a native SwiftUI card — stat columns, tappable list
rows, image carousels, composable blocks, and native charts — instead of a
code block.

**Reach for a card when the reply is:**
- A daily brief or schedule
- A metrics summary or small dashboard
- A token/usage report that needs a chart
- A tappable list of links or references
- A set of results the user will scan (search matches, options, status)
- A structured comparison

**Use plain prose instead when the reply is:**
- Narrative explanation or reasoning
- A single short answer to a direct question
- Code the user needs to copy or run (emit a real code block, not a card)
- Step-by-step instructions that read better as flowing text

**Default to a card when in doubt.** A malformed card fence falls back to a
code block — it never breaks the chat. Attempting a card is strictly
lower-risk than prose when the content is structured.

## Design principle: typographic, not decorative

Fetch cards follow a minimal, editorial register — the ChatGPT/Claude
brand-card aesthetic. The design system is pure monochrome (black on white,
hairline borders, no shadows, no chromatic accents). Cards should feel like a
well-typeset magazine, not a dashboard.

**The title commands the card. Stats and item rows carry data through type
hierarchy alone.** No icon chips, no decorative symbols, no heavy CTA
buttons. Omit `symbol` and `emoji` from every field unless you have a
specific, semantic reason not to — you almost never do.

Status ("healthy" / "blocked", "pass" / "fail") lives in the trailing
`value` text on item rows, not in icons. The footer carries the "so what" —
what the user should do next.

## Card JSON schema

All fields are optional. An object with none of `title`, `items`, `stats`,
`cards`, `chart`, or renderable `blocks` is not a card — the fence falls
back to a code block.

| Field | Type | What it does |
|---|---|---|
| `title` | string | Header title |
| `subtitle` | string | Header subtitle (smaller, muted) |
| `symbol` | string | SF Symbols icon name. **Omit by default** — cards are typographic. |
| `emoji` | string | **Not rendered** by the Fetch app. Omit. |
| `image` | string (URL) | Hero image rendered via AsyncImage above the stats |
| `url` | string (URL) | URL opened when the **whole card** is tapped |
| `footer` | string | Small caption text at the bottom |
| `stats` | array of `{label, value}` | Big-number columns (short values) or key-value rows (long values) — the renderer auto-switches based on value length |
| `items` | array of `{title, subtitle, value, symbol, url}` | Tappable list rows. Omit `symbol` — let text carry the row. |
| `cards` | array of `{title, subtitle, image, badge, url}` | Horizontal scrolling carousel of sub-cards. The **entire sub-card is the tap target** — do not use `cta`. |
| `chart` | object | Native chart body. Types: `bars`, `line`, `diverging`, `meter`, `groupedBars`, `horizontalBars`, `heatmap`. |
| `blocks` | array | Ordered composable body blocks. When present, `blocks` replaces legacy body fields (`stats`, `chart`, `items`) while `title`, `subtitle`, and `footer` still frame the card. |

### `stats[].value`

Accepts a string, integer, double, or boolean. Numbers render as clean digits;
booleans render as "Yes" / "No". Values longer than 10 characters render as
key-value rows instead of big-number columns.

### `items[].value`

Same flexible type as `stats[].value`. Renders as a muted trailing label on
the row (e.g. "3 left", "$12.50", "pass", "blocked").

### `chart`

Use `chart` when the user asked for a chart, graph, ranking, trend, or usage
breakdown. Do **not** fake charts with ASCII bars in Markdown. Emit a native
chart object inside a `card` fence.

Common chart shapes:

- `{"type":"horizontalBars","values":[1189520,912472],"labels":["cmux manager","/learn command"],"highlight":0}`
- `{"type":"bars","values":[12,18,9],"labels":["Mon","Tue","Wed"],"caption":"18"}`
- `{"type":"line","values":[5,8,7,12],"labels":["W1","W2","W3","W4"],"area":true,"smooth":true}`
- `{"type":"meter","value":78,"legend":["Used","Remaining"]}`
- `{"type":"groupedBars","series":[[12,18],[9,14]],"labels":["Mon","Tue"],"legend":["Input","Output"]}`

For token and usage reports, prefer `horizontalBars` for top sessions and
put total/prompt/completion/cache numbers in `stats`.

### `blocks`

Use `blocks` when a card needs a deliberate order, such as KPI stats, then a
chart, then a short note. Supported block types:

- `stats`: `{ "type":"stats", "stats":[{"label":"Total","value":"12.5M"}] }`
- `chart`: `{ "type":"chart", "chart":{"type":"horizontalBars","values":[...],"labels":[...]} }`
- `text`: `{ "type":"text", "title":"Summary", "text":"..." }`
- `metric`: `{ "type":"metric", "value":"42", "label":"Runs", "delta":"+8%" }`
- `progress`: `{ "type":"progress", "percent":72, "caption":"72% complete" }`
- `checklist`: `{ "type":"checklist", "steps":[{"title":"Fetched data","state":"done"}] }`
- `bars`: `{ "type":"bars", "bars":[{"label":"Prompt","value":"11.8M","fraction":0.94}] }`
- `compare`: `{ "type":"compare", "options":[{"name":"A","value":"fast","pick":true}] }`

## Patterns

For worked, copy-pasteable examples see:
- [`references/patterns.md`](references/patterns.md) — daily brief, metrics
  dashboard, link carousel, search results, status summary, and weekly review.

## Rules

1. **One card per fence.** A fence contains one JSON object. To show
   multiple cards, emit multiple `card` fences separated by a blank line or
   a short prose transition.
2. **Keep JSON valid.** The app parses with `JSONDecoder`; malformed JSON
   falls back to a code block. Use a JSON validator mentally before emitting.
3. **URLs are opened in the system browser.** Use `url` on a card (whole-card
   tap) or on an item (row tap). Carousel sub-cards take their own `url` —
   the whole sub-card is the tap target.
4. **Stats switch register automatically.** Short values (≤ 10 chars) render
   as big-number columns; longer values render as key-value rows. Don't
   force a register — let the renderer pick.
5. **No icons by default.** Omit `symbol` and `emoji` from every field.
   The renderer silently ignores unknown SF Symbol names anyway, and a
   card without icons is clean; a card with decorative icons is noise.
6. **No `cta` on carousel sub-cards.** The entire sub-card is tappable —
   the press feedback (scale + dim) signals it. Adding a `cta` field has
   no effect on rendering.
7. **Markdown still works outside cards.** A reply can mix prose, markdown,
   and multiple card fences in any order. Use cards for the structured
   payload and prose for the framing or transition between them.
8. **Real charts use `chart`, not ASCII.** If the user asks for a chart card,
   emit `chart` or a `blocks` entry with `type:"chart"`. Text bars like
   `████` are fallback prose, not the Fetch charting library.
9. **Do not substitute external chart artifacts.** In Fetch, a chart request
   should not become an SVG link, inline SVG, HTML artifact, Mermaid diagram,
   image URL, or attached file unless the user explicitly asks for that format.
   The native `card` fence is the chart delivery surface.

## Emitting

Emit cards inline in your reply. The fence opens with ` ```card ` on its own
line, the JSON follows (may span multiple lines), and the fence closes with
` ``` `. Indent the JSON for readability — the parser ignores leading
whitespace per line.

````
Here's your morning brief:

```card
{
  "title": "Today",
  "stats": [
    { "label": "Meetings", "value": 3 },
    { "label": "Tasks", "value": 12 }
  ],
  "items": [
    { "title": "Standup", "subtitle": "9:30 — Zoom", "url": "https://zoom.us/j/123" },
    { "title": "1:1 with Sam", "subtitle": "11:00 — Meet", "url": "https://meet.google.com/abc" }
  ],
  "footer": "Updated just now"
}
```

Have a good one!
````

The prose before and after the fence frames the card — don't repeat the
card's contents in prose. Let the card carry the data; let the prose carry
the warmth.

## Native chart card example

```card
{
  "title": "Fetch / Hermes Usage Report",
  "subtitle": "Last 7 days",
  "stats": [
    { "label": "Sessions", "value": 115 },
    { "label": "Total tokens", "value": "12.5M" },
    { "label": "Prompt", "value": "11.8M" },
    { "label": "Completion", "value": "706K" }
  ],
  "chart": {
    "type": "horizontalBars",
    "values": [1189520, 912472, 554273, 497174, 401152],
    "labels": ["cmux manager", "/learn command", "cmux manager", "untitled", "cmux manager"],
    "highlight": 0
  },
  "footer": "Cache read: 49.9M · cache write: 0"
}
```

## Composable blocks chart example

Use this shape when the answer needs KPI totals, a chart, and a written
summary in one card:

```card
{
  "title": "Token usage",
  "subtitle": "Last 7 days",
  "blocks": [
    {
      "type": "stats",
      "stats": [
        { "label": "Total", "value": "12.5M" },
        { "label": "Prompt", "value": "11.8M" },
        { "label": "Completion", "value": "706K" },
        { "label": "Cache read", "value": "49.9M" }
      ]
    },
    {
      "type": "chart",
      "chart": {
        "type": "horizontalBars",
        "values": [1189520, 912472, 554273, 497174, 401152],
        "labels": ["cmux manager", "/learn command", "cmux manager", "untitled", "cmux manager"],
        "highlight": 0
      }
    },
    {
      "type": "text",
      "title": "Summary",
      "text": "The largest recent session was the June 24 cmux manager run at 1.19M tokens."
    }
  ],
  "footer": "Total tokens = prompt + completion; cache is shown separately."
}
```
