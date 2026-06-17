# Hermes Inbox Plugin

`hermes_inbox` is a Hermes Agent platform plugin that lets cron jobs and
webhook direct-delivery routes send messages into the Hermes iOS app instead
of Telegram, Slack, or email.

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

After a restart, Hermes exposes `hermes_inbox` as a cron/webhook delivery
target.

## Cron Example

```bash
hermes cron create "every 15m" \
  "Run my lead scraper and send new leads." \
  --script ~/.hermes/scripts/scrape-leads.py \
  --no-agent \
  --deliver hermes_inbox
```

The plugin stores messages as Hermes sessions with `source="inbox"` and sends
an iOS proactive push linked to the created inbox thread.

## How the agent treats Fetch as a messaging channel

When enabled, the plugin registers `hermes_inbox` as a Hermes platform (a peer to
Telegram/Slack) and, on load, seeds an entry in `~/.hermes/channel_aliases.json`
so the agent *discovers* Fetch as a named target — `hermes_inbox:Fetch` — in
`send_message`, without waiting for a first message to arrive. (Hermes hides
platforms that have no known channels, and a send-only platform never discovers
one from inbound traffic, so this seed is what makes Fetch addressable.)

Combined with the home channel (`HERMES_INBOX_HOME_CHANNEL`), the agent can reach
your phone the same ways it reaches Telegram:

- `send_message(target="hermes_inbox", …)` → your Fetch app (home channel)
- `hermes cron create … --deliver hermes_inbox` → scheduled pushes
- On a fresh install with no other platform configured, Fetch is the agent's
  default place to reach you — no Telegram bot or other API setup required.

The seed is idempotent and non-destructive: it adds the channel only when absent
and never overwrites a name you've changed or other platforms' aliases.

### Optional: nudge the agent to reach out on its own

Whether the agent *chooses* to message you proactively is governed by Hermes core
behavior, not this plugin. To encourage it, add a line to `~/.hermes/SOUL.md`:

> You can reach me on my phone through Fetch. For anything time-sensitive or worth
> knowing while I'm away from the terminal — finished work, blockers, a heads-up,
> scheduled summaries — send it to the `hermes_inbox` channel.
