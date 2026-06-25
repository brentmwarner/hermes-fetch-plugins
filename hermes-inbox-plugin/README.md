# Fetch Inbox Plugin

Fetch Inbox is now part of the visible `fetch` Hermes platform. This plugin is
a compatibility/dashboard bridge for older installs that still know about the
old `hermes_inbox` target.

## Install

```bash
hermes plugins install brentmwarner/hermes-fetch-plugins/fetch-plugin --enable
hermes plugins install brentmwarner/hermes-fetch-plugins/hermes-inbox-plugin --enable
```

Restart `hermes dashboard` or `hermes gateway` after enabling the plugin.

## Configure

The iOS app can call the plugin dashboard API to write these values to
`~/.hermes/.env`:

```bash
HERMES_INBOX_ENABLED=true
HERMES_INBOX_HOME_CHANNEL=default
```

After a restart, Hermes exposes `fetch` as the canonical cron/webhook delivery
target. If the productized Fetch plugin is also installed, that plugin owns the
`fetch` registration; otherwise this compatibility plugin registers `fetch`
itself so profiles do not need a different delivery target. The old
`hermes_inbox` target is hidden unless you set:

```bash
HERMES_INBOX_REGISTER_LEGACY_PLATFORM=1
```

## Cron Example

```bash
hermes cron create "every 15m" \
  "Run my lead scraper and send new leads." \
  --script ~/.hermes/scripts/scrape-leads.py \
  --no-agent \
  --deliver fetch
```

The plugin stores messages as Hermes sessions with `source="inbox"` and sends
an iOS proactive push linked to the created inbox thread. Repeated deliveries to
the same channel slug reuse the same deterministic `inbox_<slug>` session; for
example `--deliver fetch:world-cup` always appends to `inbox_world-cup`.

## How the agent treats Fetch as a messaging channel

When Fetch setup runs, the `fetch` platform seeds an entry in
`~/.hermes/channel_aliases.json` so the agent *discovers* Fetch as a named
target — `fetch:Fetch` — in `send_message`, without waiting for a first message
to arrive. (Hermes hides platforms that have no known channels, and a send-only
platform never discovers one from inbound traffic, so this seed is what makes
Fetch addressable.)

The mixed `fetch` vs `hermes_inbox` root cause was per-profile plugin skew:
profiles with the productized Fetch plugin registered `fetch`, while profiles
with only the legacy Hermes Inbox plugin registered/seeding `hermes_inbox`.
Because Hermes derives valid cron/send targets from each profile's registered
platforms and profile-local `channel_aliases.json`, the same Fetch app appeared
under different platform names. The plugin now normalizes both platform prefixes
to the same channel slug and seeds only canonical `fetch` aliases so normal users
can target Fetch consistently.

Combined with the home channel (`HERMES_INBOX_HOME_CHANNEL`), the agent can reach
your phone the same way it reaches Telegram:

- `send_message(target="fetch", ...)` -> your Fetch app (home channel)
- `hermes cron create ... --deliver fetch` -> scheduled pushes
- On a fresh install with no other platform configured, Fetch is the agent's
  default place to reach you — no Telegram bot or other API setup required.

The seed is idempotent and non-destructive: it adds the channel only when absent
and never overwrites a name you've changed or other platforms' aliases.

### Optional: nudge the agent to reach out on its own

Whether the agent *chooses* to message you proactively is governed by Hermes core
behavior, not this plugin. To encourage it, add a line to `~/.hermes/SOUL.md`:

> You can reach me on my phone through Fetch. For anything time-sensitive or worth
> knowing while I'm away from the terminal - finished work, blockers, a heads-up,
> scheduled summaries - send it to the `fetch` channel.
