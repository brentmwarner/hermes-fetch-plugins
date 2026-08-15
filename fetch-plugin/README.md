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
hermes setup                # choose Fetch, paste the Fetch setup code, then scan/paste the setup link
```

Before `hermes setup`, open Fetch on the phone, sign in, and create a setup
code. That code enrolls the agent into your Fetch account; the relay link shown
afterward pairs this phone to the enrolled agent. No Apple account, no `.p8`, no
core edits, no public dashboard URL, and no browser tab to keep open.

## Running on a VPS or headless server

Fully supported, and nothing needs to be exposed to the internet: the agent
dials **outbound** to the relay (`wss://push.tryfetchapp.com`) and the phone
comes in through that reverse tunnel.

- Keep the Hermes dashboard bound to **`127.0.0.1`** (the default). Binding a
  non-loopback host (`0.0.0.0`, the VPS IP) makes Hermes auto-engage its login
  gate, and the Fetch app will be locked out with "This server uses a login" —
  `hermes setup` warns about this. For browser access from your laptop, use SSH
  forwarding (`ssh -L 9119:127.0.0.1:9119 <vps>`) instead.
- Do **not** set `GATEWAY_RELAY_URL`, `gateway.relay_url`, or any
  `HERMES_RELAY_*` variables for Fetch — none are client-side settings. The
  `HERMES_RELAY_*` names configure the relay **server**, and `GATEWAY_RELAY_URL`
  activates an unrelated experimental gateway connector that will just dial the
  push relay's `/relay` path forever and get 403s.
- If you run your **own** `hermes dashboard`/`gateway` process (rather than
  letting the plugin's headless runtime own it), it must share the dashboard
  session token with the Fetch tunnel or the app fails with "token not
  accepted". `hermes setup` now pins a `HERMES_DASHBOARD_SESSION_TOKEN` in
  `~/.hermes/.env` for this; **restart your own dashboard after setup** so it
  reloads `.env` and picks up the same token.

### Computer view (Mac, Windows, and Linux)

When browser or desktop work reaches a step only the person should complete
(MFA, CAPTCHA, private information, payment approval, or legal certification),
the Fetch platform guidance tells Hermes to call `clarify` with `I'm done` and
`Skip`. The iOS app turns that blocking request into an **Action needed** card
with the real desktop frame and Take over controls. Marking the step done or
skipping it answers the existing clarify request so the paused agent can
continue; no private input needs to be pasted into chat.

Fetch can watch the computer Hermes is using and, after confirmation, hand
control to the iPhone. On Linux the default screen is a portable Ubuntu
container (Docker or Podman) that Hermes drives from the host. Mac and Windows
keep their native desktops; the same Ubuntu image is an optional extra there,
not a replacement for Xcode, Simulator, or real Windows apps. The plugin
connects only to a loopback VNC port, then carries the pixels and input over
the existing authenticated outbound Fetch relay. Do not build a second relay
or expose VNC to the network.

VNC transport authentication is completed on the host before any pixels are
forwarded. Its dedicated password stays in the owner-only Hermes environment
file and is never sent to the Fetch relay or iPhone. Consequently, opening an
already signed-in desktop does not show a second username/password form on the
phone. If the operating system itself is locked, its real lock screen remains
visible and the person can unlock it after taking control.

On Hermes versions that advertise foreground delivery in the public
`computer_use` schema, input requested from a Fetch conversation is delivered
to the visible foreground window so the streamed desktop and the agent's target
cannot drift apart. Older Hermes versions keep their original input behavior
instead of receiving unsupported arguments. Window moves and resizes use
cua-driver's verified frame operation
(`fetch_window_control`) instead of assuming a posted title-bar drag worked;
the operation succeeds only after an independent geometry readback and a
post-move visibility check. Other Hermes surfaces keep the upstream
background-computer behavior.

Every session starts in **Watch** mode. Choosing **Control** asks for
confirmation and stops the current Hermes turn started from Fetch before the
phone can send input. The MVP cannot pause a separate Hermes process or turn
started from another client, so do not run two computer-controlling Hermes
sessions against the same desktop.

#### Mac

Use macOS's built-in Screen Sharing server:

1. Open **System Settings → General → Sharing** and turn on **Screen Sharing**.
2. Open its info panel, allow only the intended macOS account, enable **VNC
   viewers may control screen with password**, and set a dedicated password.
3. Pair Fetch with `hermes setup`, then configure and verify the complete path:

   ```bash
   cd <plugin-checkout>/fetch-plugin/macos
   ./check-computer.sh
   ```

The check prompts once on the Mac for that dedicated VNC password, verifies it
against the loopback Screen Sharing service, and saves it in the owner-only
Hermes environment file. It is not the user's macOS account password. Turning
the display off does not remove the shared framebuffer, but the Mac must remain
awake. Enable **Wake for network access** and prevent automatic system sleep
while on power if this Mac should remain reachable unattended. For isolation
from personal apps and files, run Hermes and Screen Sharing in a dedicated
macOS user account. Docker Desktop running the Ubuntu Fetch computer is an
optional extra for a virtual Linux desktop; it is not a replacement for the
real Mac. Xcode and Simulator cannot run in Ubuntu. iOS work stays on this
Mac.

#### Windows

Use UltraVNC Server to share the signed-in Windows desktop without a container:

1. Install **UltraVNC Server** and open its **Admin Properties**.
2. Enable **Accept Socket Connections**, **Allow Loopback Connections**, and
   **Loopback Only**. Use display `0` / port `5900`, turn off the Java/HTTP
   viewer and file transfer, and set a dedicated VNC password.
3. Restart the UltraVNC Server service, pair Fetch with `hermes setup`, then
   configure and verify the complete path from
   PowerShell:

   ```powershell
   cd <plugin-checkout>\fetch-plugin\windows
   .\check-computer.ps1
   ```

The check prompts once on the PC for that dedicated UltraVNC password, verifies
it against the loopback server, and saves it in the owner-only Hermes
environment file. It is not the user's Windows account password. Keep the PC
awake for unattended use. For isolation from personal apps and files, use a
dedicated Windows account. The plugin rejects non-loopback targets even if the
VNC server is accidentally configured more broadly, but **Loopback Only**
prevents any other program on the network from reaching UltraVNC directly.
Docker Desktop running the Ubuntu Fetch computer is an optional extra, not a
replacement for real Windows applications.

#### Linux (default): Fetch computer container

On Fedora, Ubuntu, other Linux desktops, and a VPS, the default computer is
one Ubuntu container. Hermes stays on the host. The plugin starts TigerVNC +
XFCE inside the container, maps RFB only to `127.0.0.1:5901`, and reuses the
existing Fetch loopback bridge. Fedora Wayland is a first-class case: do not
scrape the physical login session.

Install Docker or Podman first. Setup fails closed if neither engine is
available; it does not silently `apt`/`dnf` XFCE onto the host. After pairing
Fetch, update the plugin and run:

```bash
cd ~/.hermes/plugins/fetch/linux-computer
./manage-computer.sh bootstrap
```

The bootstrap builds the image if needed, starts the `fetch-computer`
container, waits until RFB answers on `127.0.0.1:5901`, persists
`HERMES_FETCH_COMPUTER_TARGET=tcp://127.0.0.1:5901`,
`HERMES_FETCH_COMPUTER_NAME=Fetch computer`, and
`HERMES_FETCH_COMPUTER_KIND=Virtual Linux desktop`, then starts the existing
bridge and fail-closes on `GET /v1/agents/computer/status` the same way
`computer_setup.py` already does. On Linux it also sets `DISPLAY=:1` and
`AGENT_BROWSER_HEADED=1` so Hermes `browser_*` / `computer_use` windows open
on the virtual desktop. One Hermes install uses one computer container
(display `:1`). Extra virtual displays can come later.

The default backdrop is the Fetch brand landscape shipped at
`linux-computer/branding/wallpaper.png` — a soft-focus view of rolling green
hills, warm golden-hour light, and a pale sky. Replace that file and rebuild
the image to change the desktop background without a redesign.

The VNC port is deliberately private. On Linux the container uses host
networking so TigerVNC can bind `127.0.0.1:5901` with `-localhost`. On Docker
Desktop the manager publishes only `127.0.0.1:5901`. Never publish VNC on
`0.0.0.0` or open port 5901 in a firewall.

Useful lifecycle commands:

```bash
./manage-computer.sh status
./manage-computer.sh uninstall
```

Uninstall (and `computer_setup.py --disable`) stop and remove the container,
terminate the Fetch computer bridge, and clear the persisted computer/display
settings so later plugin starts do not re-advertise this host.

#### Existing Xorg Linux desktop (opt-in)

Only if you already run an **Xorg** session and want Fetch to scrape that
physical monitor. Wayland users should use the container above. The included
service uses TigerVNC's `x0vncserver` to share the existing X11 desktop.

On Ubuntu, sign into an **Xorg** session, open a terminal in that desktop, and
run:

```bash
sudo apt update
sudo apt install tigervnc-scraping-server

cd <plugin-checkout>/fetch-plugin/linux-desktop
./manage-user-service.sh install
```

The installer writes the loopback target and current `DISPLAY` to the Hermes
environment, starts the dedicated Fetch computer bridge, and exits successfully
only after the VNC server and relay uplink both answer readiness checks.

The display can be powered off while the X11 session remains active. Disable
automatic system suspend for unattended use. A disconnected display can cause
some graphics drivers to remove the framebuffer; keep the display connected or
use a display emulator.

Lifecycle commands are `./manage-user-service.sh status` and
`./manage-user-service.sh uninstall`. Uninstall stops the X11 VNC service,
terminates the Fetch computer bridge, and removes the persisted computer
target so later plugin starts do not re-advertise this desktop.

#### Host virtual desktop without containers (opt-in)

`linux-vps/` still installs TigerVNC + XFCE on the host via `apt` or `dnf`.
That is no longer the default Linux path. Use it only when you cannot run
Docker or Podman. The container setup above is what Fedora Wayland and VPS
hosts should run.

## Reconfigure or reset

Run `hermes setup` again, choose Fetch, and confirm the reconfigure prompt. The
plugin mints a fresh setup link every time.

If Fetch was disabled and later re-enabled, old `HERMES_FETCH_*` delivery values
may still exist in `~/.hermes/.env`; they are harmless and do not count as a
connected Fetch setup by themselves. The setup status is based on the relay
pairing stored at `~/.hermes/push/fetch-relay.json`, because that is the state
the phone actually needs to reach the agent.

## Configuration

Most users set nothing — Fetch setup configures delivery and the tunnel for you.
After a relay pairing exists, the tunnel starts by default even if the legacy
enablement flag is missing.
All env vars are `HERMES_FETCH_*`; there is no separate inbox product.

**Public knobs:**

| Env var | Default | Purpose |
| --- | --- | --- |
| `HERMES_FETCH_RELAY_URL` | hosted relay (`https://push.tryfetchapp.com`) | Point at a different / local relay. |
| `HERMES_FETCH_ENROLLMENT_TOKEN` | _(none)_ | One-time setup code from the signed-in Fetch app. Usually pasted interactively during setup. |
| `HERMES_FETCH_RELAY_REGISTRATION_TOKEN` | _(none)_ | Operator/private relay registration token. Public Fetch users should not need this. |
| `HERMES_FETCH_TUNNEL_ENABLED` | auto after Fetch relay setup | Keep the agent-side reverse tunnel active for relay pairing. Set `0`/`false` only to force-disable it. |
| `HERMES_FETCH_TUNNEL_DISABLE_DASHBOARD_AUTOSTART` | _(unset)_ | Opt out if you manage the local Hermes dashboard/API process yourself. |
| `HERMES_FETCH_COMPUTER_TARGET` | _(unset)_ | Enable computer viewing with a loopback-only VNC target, normally `tcp://127.0.0.1:5900` for Mac/Windows/physical Linux or `tcp://127.0.0.1:5901` for the Linux container / virtual Linux desktop. Fetch rejects LAN/public targets. |
| `HERMES_FETCH_COMPUTER_NAME` | host name | Friendly computer name shown in the Fetch viewer. The Linux container setup saves `Fetch computer`. |
| `HERMES_FETCH_COMPUTER_KIND` | inferred from OS | Secondary label such as `Mac desktop`, `Windows desktop`, `Linux desktop`, or `Virtual Linux desktop`. |
| `HERMES_FETCH_COMPUTER_VNC_PASSWORD` | _(unset)_ | Dedicated VNC transport password saved by Mac/Windows computer setup. It is consumed only by the host-side loopback bridge and is never sent to the relay or phone. |
| `HERMES_FETCH_COMPUTER_WS_URL` | _(unset)_ | Legacy loopback websockify target. Still accepted for compatibility; new setups should use `HERMES_FETCH_COMPUTER_TARGET`. |

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
thread. The job id comes from the scheduler's send metadata (`{"job_id": ...}`)
first, with the wrapped `Cronjob Response:` content header as fallback — so the
split survives `cron.wrap_response: false`.

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

### File attachments

Fetch turns can include any safe, regular host file up to 100 MB. The agent uses
Hermes' standard attachment contract by placing the completed file on its own
line in the response:

```text
Your report is ready.
MEDIA:/absolute/path/to/report.pdf
```

The iOS app renders supported images and videos inline. PDFs, office documents,
text/data files, archives, and unknown or extensionless files render as a file
chip that opens Quick Look and offers the system Save/Share sheet. The first
open needs the paired agent online and the source file still present; after a
successful download, the app can reopen its cached copy offline. Fetch setup
injects this contract automatically, and proactive/cron delivery preserves
Hermes media paths instead of dropping them.

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
