# Fetch Push Relay

Minimal APNs relay for the Fetch iOS app.

The relay does not run Hermes Agent and does not store conversations. It stores
anonymous agent credentials, APNs device tokens, notification preferences, and
push delivery metadata. Notification title/body copy is forwarded to APNs by
default.

## Local Run

```bash
cd server/push-relay
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
scripts/start-local-relay.sh
```

Configure APNs credentials in the relay environment:

```bash
export HERMES_RELAY_APNS_KEY_PATH=/secure/path/AuthKey_XXXXXXXXXX.p8
export HERMES_RELAY_APNS_KEY_ID=XXXXXXXXXX
export HERMES_RELAY_APNS_TEAM_ID=TEAMID1234
```

Point the [`fetch` plugin](../../fetch-plugin) at it during development:

```bash
export HERMES_FETCH_RELAY_URL=http://127.0.0.1:8787
# allow anonymous agent registration locally (prod requires a token):
export HERMES_RELAY_ALLOW_OPEN_REGISTRATION=true
export HERMES_RELAY_ENABLE_TUNNEL=true
```

For local testing, the script automatically sources `~/.hermes/.env`, uses
`server/push-relay/data/push-relay.db`, and forwards notification title/body
copy.

Stop the local relay:

```bash
scripts/stop-local-relay.sh
```

## Deploy

The relay is a stateful long-running service (holds an APNs HTTP/2 connection +
a SQLite store), so deploy it to a container host with a persistent disk —
**Fly.io / Railway / Render / a VM**, not a serverless platform (ephemeral FS
would lose the SQLite DB and the connection reuse). See `.env.example` for the
full env reference and `Dockerfile` / `fly.toml` for the build.

### Railway (recommended)

`railway.json` builds from the `Dockerfile` and health-checks `/healthz`. Run
`railway up` from `server/push-relay/` so Railway sees this `railway.json`.
Before promoting production, verify the build logs show `load build definition
from Dockerfile`, `python:3.12-slim`, and the Uvicorn relay image instead of a
static/default builder.

```bash
railway login                                   # interactive (browser)
cd server/push-relay
railway init --name fetch-push-relay            # create project, link this dir
railway up --detach                             # first build + deploy
railway volume add -m /data                      # persistent SQLite (mounts /data)
# Secrets — .p8 via stdin so it never lands in shell history:
cat /path/to/AuthKey_XXXXXXXXXX.p8 | railway variable set HERMES_RELAY_APNS_KEY --stdin
railway variable set \
  "HERMES_RELAY_APNS_KEY_ID=XXXXXXXXXX" \
  "HERMES_RELAY_APNS_TEAM_ID=XXXXXXXXXX" \
  "HERMES_RELAY_REGISTRATION_TOKEN=$(openssl rand -hex 24)" \
  "HERMES_RELAY_SECRET_PEPPER=$(openssl rand -hex 24)" \
  "HERMES_RELAY_ENABLE_TUNNEL=true" \
  "HERMES_RELAY_ALLOWED_BUNDLE_IDS=com.brentwarner.fetch"   # redeploys with creds + volume
railway domain                                   # public HTTPS URL
```

`HERMES_RELAY_DATABASE_PATH` defaults to `/data/push-relay.db` (the Dockerfile
sets it), matching the volume mount. Capture the `HERMES_RELAY_REGISTRATION_TOKEN`
— each agent's plugin must send the same value as `HERMES_FETCH_RELAY_REGISTRATION_TOKEN`.

### Fly.io

```bash
cd server/push-relay
fly launch --no-deploy --copy-config --name fetch-push-relay
fly volumes create relay_data --size 1 --region iad
fly secrets set \
  HERMES_RELAY_APNS_KEY="$(cat AuthKey_XXXXXXXXXX.p8)" \
  HERMES_RELAY_APNS_KEY_ID=XXXXXXXXXX \
  HERMES_RELAY_APNS_TEAM_ID=XXXXXXXXXX \
  HERMES_RELAY_REGISTRATION_TOKEN="$(openssl rand -hex 24)" \
  HERMES_RELAY_SECRET_PEPPER="$(openssl rand -hex 24)" \
  HERMES_RELAY_ENABLE_TUNNEL=true
fly deploy
fly open /healthz   # -> {"ok":true,"apns_configured":true}
```

### Any Docker host

```bash
docker build -t fetch-push-relay server/push-relay
docker run -p 8080:8080 -v relay_data:/data \
  -e HERMES_RELAY_APNS_KEY="$(cat AuthKey.p8)" \
  -e HERMES_RELAY_APNS_KEY_ID=XXXXXXXXXX -e HERMES_RELAY_APNS_TEAM_ID=XXXXXXXXXX \
  -e HERMES_RELAY_REGISTRATION_TOKEN="$(openssl rand -hex 24)" \
  -e HERMES_RELAY_SECRET_PEPPER="$(openssl rand -hex 24)" \
  -e HERMES_RELAY_ENABLE_TUNNEL=true \
  fetch-push-relay
```

Then put it behind HTTPS at your relay domain (e.g. `push.tryfetchapp.com`) and
set that as the plugin's `HERMES_FETCH_RELAY_URL` (or bake it as the default in
`fetch-plugin/_relay.py`).

## Production checklist

- **Secrets:** APNs `.p8` only in a secret manager / `fly secrets` — never in git.
- **HTTPS** in front, plus **edge rate limiting / WAF** (the app has in-process
  rate limiting as a backstop, not a substitute).
- **`HERMES_RELAY_REGISTRATION_TOKEN` is mandatory** — the relay fails closed
  (503 on `/v1/agents/register`) without it unless `ALLOW_OPEN_REGISTRATION` is
  set. Treat any token baked into the public plugin as public; pair with quotas.
- **Notification body privacy:** `HERMES_RELAY_ALLOW_CUSTOM_BODY=true` forwards
  plugin-provided title/body copy through APNs. Set it to `false` to force
  generic lock-screen copy when conversation text must stay off the relay.
- **`HERMES_RELAY_ALLOWED_BUNDLE_IDS`** = your real App ID(s) only.
- **`HERMES_RELAY_ENABLE_TUNNEL=true`** is required for relay pairing and
  app-to-agent streaming.
- Verify the `.p8` is APNs-enabled for that App ID in the Apple portal, and test
  one real push to a TestFlight device.

## Scaling & managed-delivery option

SQLite (WAL) is fine for a single-instance pilot. For horizontal scale, move the
store to **Postgres** and the rate-limit state to a shared backend.

The relay talks to APNs directly today. An alternative for the **delivery layer**
is to swap APNs for a managed push backend — **FCM, OneSignal, or Amazon SNS** —
which offloads APNs connection management, token lifecycle, and retries. The
relay would still front it for per-`agent_id` multi-tenancy (those services
assume one app backend, not thousands of independent agents). This is a contained
change (only the APNs-send path) and worth it once delivery ops cost grows; it is
not needed to launch.
