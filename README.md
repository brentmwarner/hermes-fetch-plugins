# Hermes Fetch Plugins

Public Hermes Agent plugin and relay used by the Fetch iOS app.

## Services

- `server/push-relay`: public Fetch relay service. It owns APNs delivery,
  agent enrollment, pairing-token minting, and the reverse WebSocket tunnel used
  by the iOS app when `HERMES_RELAY_ENABLE_TUNNEL=true`.

## Plugin

- `fetch-plugin`: the single first-class Fetch plugin. It registers `fetch` as a
  Hermes platform with relay pairing (link + QR), the agent-side reverse tunnel,
  push notifications, inbox/cron/webhook delivery, and generative-UI `card`
  guidance. `fetch` is the one user-facing setup and delivery target — there is
  no separate inbox product to install or configure.

Install the single plugin:

```bash
hermes plugins install brentmwarner/hermes-fetch-plugins/fetch-plugin --enable
hermes gateway restart
hermes setup
```

In setup, choose Fetch and scan/paste the generated relay link in the iOS app.
Relay setup starts the local headless runtime automatically; if you run
`hermes dashboard` separately, restart it only if you want that visible
dashboard process to pick up plugin changes too.

Use `fetch` for scheduled or message delivery:

```bash
hermes cron create "every 15m" "Send my summary to Fetch." --deliver fetch
```

## Configuration

The default hosted relay is `https://push.tryfetchapp.com`. Most users do not
need to set any environment variables: Fetch setup auto-configures delivery and
the reverse tunnel, and starts a headless local Hermes API process so the phone
needs no public dashboard URL, Tailscale, or open browser tab.

Optional environment variables:

| Name | Purpose |
| --- | --- |
| `HERMES_FETCH_RELAY_URL` | Override the hosted Fetch push relay URL. |
| `HERMES_FETCH_RELAY_REGISTRATION_TOKEN` | Enrollment token, if your relay requires one. |
| `HERMES_FETCH_TUNNEL_ENABLED` | Enabled automatically by Fetch relay setup so the agent keeps a reverse tunnel to the hosted relay. |
| `HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART` | Opt out of Fetch's headless local dashboard/API autostart if you run that process yourself. |

Delivery enablement and the home channel are configured for you by setup; see
[`fetch-plugin/README.md`](fetch-plugin/README.md) for the advanced internal
knobs (`HERMES_FETCH_HOME_CHANNEL`, `HERMES_FETCH_STORE_HOME`).
