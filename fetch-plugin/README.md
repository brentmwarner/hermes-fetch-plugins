# Fetch Push plugin

A single Hermes Agent plugin that gives the Fetch iOS app push notifications —
without patching Hermes core and without any Apple Developer credentials on the
user's host. It is the productized replacement for the old approach of editing
`hermes_cli/web_server.py` / `tui_gateway/server.py` (which `hermes update`
wipes and which required each user to hold the `.p8`).

## How it works

```
 iPhone (Fetch app)        User's Hermes agent (this plugin)         Your infra
 ─────────────────         ─────────────────────────────────        ──────────────
  register token  ──▶  /api/plugins/fetch/register ──┐
   (session token)     (dashboard half, plugin_api.py)│ proxy        ┌────────────┐
                                                       └────────────▶│ push relay │
   🔔  banner    ◀───────  post_llm_call hook  ───────────POST──────▶│ holds the  │──▶ APNs
                          (turn finished → "replied")   /push/events │ one .p8    │
                          pre_approval_request hook ────────────────▶│ fans out   │
                          (needs attention)                          └────────────┘
```

The plugin is **one installed package scanned by two independent systems in two
processes**, coupled only through the relay:

- **Runtime half** (`plugin.yaml` + `__init__.py`): loaded by the agent's
  `PluginManager` in the TUI / gateway / dashboard-chat process. `register(ctx)`
  wires two hooks:
  - `post_llm_call` — fires once per completed turn on **every** surface (phone,
    web dashboard, terminal), so a reply typed anywhere notifies the phone.
  - `pre_approval_request` — fires when the agent blocks on an approval /
    clarifying question / secret.
  Each hook fire-and-forgets an HTTPS POST to the relay's `/v1/push/events`.
- **Dashboard half** (`dashboard/manifest.json` + `dashboard/plugin_api.py`):
  loaded by the dashboard `web_server` process and auto-mounted at
  `/api/plugins/fetch/`, behind the dashboard's session-token auth. `/register`
  and `/unregister` proxy device tokens straight to the relay (no token DB on
  the host).
- `_relay.py` is the shared relay client, loaded **by file path** from both
  halves (they don't share a Python import).

The relay holds the single Fetch APNs key and is the only component that talks
to Apple. This host only ever stores an anonymous, per-agent `agent_id` +
`agent_secret` (in `~/.hermes/push/fetch-relay.json`), minted automatically on
first use.

## Install (per user)

```bash
hermes plugins install brentmwarner/hermes-fetch-plugins/fetch-plugin --enable
# Restart BOTH so the hooks load and the route mounts (neither hot-reloads):
hermes gateway restart      # (and restart `hermes dashboard` if running separately)
```

Then open Fetch on the phone and allow notifications. No Apple account, no
`.p8`, no core edits.

## Configuration

| Env var | Default | Purpose |
| --- | --- | --- |
| `HERMES_FETCH_RELAY_URL` | hosted relay (`https://push.tryfetchapp.com`) | Point at a different / local relay. |
| `HERMES_FETCH_RELAY_REGISTRATION_TOKEN` | _(none)_ | Enrollment token, if the relay requires one. |

For local development, run the relay from `server/push-relay/` and set
`HERMES_FETCH_RELAY_URL=http://127.0.0.1:8787`.

## Notes & limits

- **Dual restart required.** Hooks load once at agent startup; the dashboard
  route mounts once at `web_server` import. Installing does nothing until both
  processes restart. The iOS app shows a "plugin not installed / restart Hermes"
  state when `/api/plugins/fetch/register` returns 404.
- **Under-notification.** `post_llm_call` only fires when a turn ends with a
  non-empty final response and wasn't interrupted. Tool-only / interrupted /
  empty turns don't push; genuine "needs attention" stalls are covered by the
  approval hook. Notifying on a silent stall would need an upstream core change.
- **Every surface pushes.** Because the trigger is `post_llm_call` (agent core),
  a reply to a Telegram/Slack message also pushes the phone. The relay de-dupes
  a short window and the app suppresses the banner for the thread you're already
  viewing; per-category prefs (`replies` / `attention` / `proactive` / `sound`)
  are honored server-side.
