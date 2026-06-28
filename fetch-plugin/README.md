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
  register token  ──▶  relay tunnel ──▶ /api/plugins/fetch/register ─┐
   (session token)     (headless API)    (dashboard half)             │ proxy        ┌────────────┐
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
- **Headless relay runtime** (`_runtime.py`): successful relay setup starts a
  background loopback-only Hermes dashboard/API process with no browser window.
  The reverse tunnel stays alive after `hermes setup` exits, so the phone does
  not need a public dashboard, Tailscale, or an open browser tab. Setup waits
  for the relay to report this agent's tunnel online before it shows the QR/link;
  if the tunnel cannot come online, setup hides the link so the app does not
  receive an unusable pairing.
- `_relay.py` is the shared relay client, loaded **by file path** from both
  halves (they don't share a Python import).

The relay holds the single Fetch APNs key and is the only component that talks
to Apple. This host only ever stores an anonymous, per-agent `agent_id` +
`agent_secret` (in `~/.hermes/push/fetch-relay.json`), minted automatically on
first use.

## Install (per user)

```bash
hermes plugins install brentmwarner/hermes-fetch-plugins/fetch-plugin --enable
hermes gateway restart      # (and restart `hermes dashboard` if running separately)
hermes setup                # choose Fetch, then scan/paste the setup link
```

Then open Fetch on the phone and allow notifications. No Apple account, no
`.p8`, no core edits, no public dashboard URL, and no browser tab to keep open.

## Configuration

Most users set nothing — Fetch setup configures delivery and the tunnel for you.
After a relay pairing exists, the tunnel starts by default even if the legacy
enablement flag is missing.
All env vars are `HERMES_FETCH_*`; there is no separate inbox product.

**Public knobs:**

| Env var | Default | Purpose |
| --- | --- | --- |
| `HERMES_FETCH_RELAY_URL` | hosted relay (`https://push.tryfetchapp.com`) | Point at a different / local relay. |
| `HERMES_FETCH_RELAY_REGISTRATION_TOKEN` | _(none)_ | Enrollment token, if the relay requires one. |
| `HERMES_FETCH_TUNNEL_ENABLED` | auto after Fetch relay setup | Keep the agent-side reverse tunnel active for relay pairing. Set `0`/`false` only to force-disable it. |
| `HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART` | _(unset)_ | Opt out if you manage the local Hermes dashboard/API process yourself. |

**Internal / advanced** (written by Fetch setup or the `/api/plugins/fetch/inbox/enable`
dashboard route; rarely set by hand):

| Env var | Default | Purpose |
| --- | --- | --- |
| `HERMES_FETCH_DELIVERY_ENABLED` | set by Fetch setup | Enable Fetch as a cron/webhook delivery target. |
| `HERMES_FETCH_HOME_CHANNEL` | `default` | Default Fetch channel used by bare `--deliver fetch`. |
| `HERMES_FETCH_STORE_HOME` | running profile home | Relay-paired Hermes home whose `state.db` receives delivery sessions (multi-profile setups). |

For local development, run the relay from `server/push-relay/` and set
`HERMES_FETCH_RELAY_URL=http://127.0.0.1:8787`.

## Inbox delivery and thread affinity

Fetch is the canonical Hermes delivery surface for proactive and cron output:

```bash
hermes cron create "every 15m" "Summarize the World Cup" --deliver fetch:world-cup
```

Delivery channels are normalized before persistence: `fetch:world-cup` resolves
to the deterministic Hermes session `inbox_world-cup`, so repeated deliveries to
the same channel append to the same app thread. Bare `--deliver fetch` uses
`HERMES_FETCH_HOME_CHANNEL` (default `default`). Cron responses delivered to the
home channel are split by cron job id (`inbox_cron-<job-id>`) so each scheduled
job gets a stable thread instead of all proactive output collapsing into one
thread.

(`inbox` here is an internal wire tag — the session `source` value and
`inbox_<slug>` session-id prefix the iOS app keys its inbox off. The user only
ever sees and targets `fetch`.)

This channel-thread affinity is enforced in the plugin because Fetch owns the
phone-side inbox UX; end users should not need to manually pick Hermes thread ids
or understand profile-specific platform names.

## Fetch as a messaging channel

When Fetch setup runs, the plugin seeds an entry in
`~/.hermes/channel_aliases.json` so the agent *discovers* Fetch as a named target
(`fetch`) in `send_message`, without waiting for a first inbound message. (Hermes
hides platforms that have no known channels, and a send-only platform never
discovers one from inbound traffic, so this seed is what makes Fetch
addressable.) One alias is seeded per Hermes profile, so each agent is reachable
as its own DM target — `fetch:researcher` → "Researcher".

- `send_message(target="fetch", ...)` → your Fetch app (home channel)
- `hermes cron create ... --deliver fetch` → scheduled pushes
- On a fresh install with no other platform configured, Fetch is the agent's
  default place to reach you — no Telegram bot or other API setup required.

The seed is idempotent and non-destructive: it adds a channel only when absent
and never overwrites a name you've changed or other platforms' aliases.

To nudge the agent to reach out on its own, add a line to `~/.hermes/SOUL.md`:

> You can reach me on my phone through Fetch. For anything time-sensitive or
> worth knowing while I'm away from the terminal — finished work, blockers, a
> heads-up, scheduled summaries — send it to the `fetch` channel.

## Notes & limits

- **Restart required after install.** Hooks load once at agent startup. Fetch
  relay setup starts a headless dashboard/API process for the app path; restart
  a separately managed `hermes dashboard` only if you deliberately disabled
  Fetch autostart.
- **Under-notification.** `post_llm_call` only fires when a turn ends with a
  non-empty final response and wasn't interrupted. Tool-only / interrupted /
  empty turns don't push; genuine "needs attention" stalls are covered by the
  approval hook. Notifying on a silent stall would need an upstream core change.
- **Every surface pushes.** Because the trigger is `post_llm_call` (agent core),
  a reply to a Telegram/Slack message also pushes the phone. The relay de-dupes
  a short window and the app suppresses the banner for the thread you're already
  viewing; per-category prefs (`replies` / `attention` / `proactive` / `sound`)
  are honored server-side.
