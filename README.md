# Hermes Fetch Plugins

Public Hermes Agent plugins used by the Fetch iOS app.

## Services

- `server/push-relay`: public Fetch relay service. It owns APNs delivery,
  agent enrollment, pairing-token minting, and the reverse WebSocket tunnel used
  by the iOS app when `HERMES_RELAY_ENABLE_TUNNEL=true`.

## Plugins

- `fetch-plugin`: registers the Fetch app for iOS push notifications and sends reply/attention/proactive events through the hosted Fetch relay.
- `hermes-inbox-plugin`: adds a `hermes_inbox` delivery target so cron jobs, webhooks, and automations can deliver messages into the Fetch app.

Install both plugins for the complete Fetch notification and inbox experience:

```bash
hermes plugins install brentmwarner/hermes-fetch-plugins/fetch-plugin --enable
hermes plugins install brentmwarner/hermes-fetch-plugins/hermes-inbox-plugin --enable
hermes gateway restart
```

If you run `hermes dashboard` separately, restart it too so the plugin dashboard routes are mounted.

## Configuration

The default hosted relay is `https://push.tryfetchapp.com`. Most users do not need to set any environment variables.

Optional environment variables:

| Name | Purpose |
| --- | --- |
| `HERMES_FETCH_RELAY_URL` | Override the hosted Fetch push relay URL. |
| `HERMES_FETCH_RELAY_REGISTRATION_TOKEN` | Enrollment token, if your relay requires one. |
| `HERMES_INBOX_ENABLED` | Enable Hermes Inbox as a delivery target. |
| `HERMES_INBOX_HOME_CHANNEL` | Default inbox channel used by `deliver=hermes_inbox`. |
