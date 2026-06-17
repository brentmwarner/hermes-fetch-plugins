# Hermes Inbox Plugin

`hermes_inbox` is a Hermes Agent platform plugin that lets cron jobs and
webhook direct-delivery routes send messages into the Hermes iOS app instead
of Telegram, Slack, or email.

## Install Locally

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
