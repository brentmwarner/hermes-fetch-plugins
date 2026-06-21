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
hermes setup
```

In setup, choose Fetch and scan/paste the generated link in the iOS app. Relay setup starts the local headless runtime automatically; if you run `hermes dashboard` separately, restart it only if you want the visible dashboard process to pick up plugin changes too.

## Configuration

The default hosted relay is `https://push.tryfetchapp.com`. Most users do not need to set any environment variables. Fetch relay setup starts a headless local Hermes API process so the phone does not need a public dashboard URL, Tailscale, or an open browser tab.

Optional environment variables:

| Name | Purpose |
| --- | --- |
| `HERMES_FETCH_RELAY_URL` | Override the hosted Fetch push relay URL. |
| `HERMES_FETCH_RELAY_REGISTRATION_TOKEN` | Enrollment token, if your relay requires one. |
| `HERMES_FETCH_TUNNEL_ENABLED` | Enabled automatically by Fetch relay setup so the agent keeps a reverse tunnel to the hosted relay. |
| `HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART` | Opt out of Fetch's headless local dashboard/API autostart if you run that process yourself. |
| `HERMES_INBOX_ENABLED` | Enable Hermes Inbox as a delivery target. |
| `HERMES_INBOX_HOME_CHANNEL` | Default inbox channel used by `deliver=hermes_inbox`. |
