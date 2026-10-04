# Deployment

Two supported paths. They run the same image and the same schema, so moving
between them is a matter of pointing `DATABASE_URL` somewhere else.

---

## A. Self-hosted with Docker Compose (free)

Railway removed its free service tier, so this is the no-cost option. It needs
a machine that stays on — a home server, a mini PC, or the cheapest VPS you
can find. Rendering wants at least 2 cores and 4 GB of RAM.

### 1. Configure

```bash
cp .env.example .env
```

Set at minimum:

```
POSTGRES_PASSWORD=<a long random string>
API_TOKEN=<a long random string>
GEMINI_API_KEY=<your key>
CONTACT_EMAIL=you@example.com
```

`CONTACT_EMAIL` is not optional in spirit: Wikimedia and Crossref both ask
bots to identify themselves, and supplying an address is the difference
between the polite request pool and being throttled.

### 2. Build and start

```bash
docker compose up -d --build
```

The build downloads the Piper narration voice (~60 MB) and bakes it into the
image, so restarts never re-fetch it.

### 3. Verify

```bash
docker compose exec app python -m echoes.cli doctor
```

Expect `"problems": []`. Anything listed there is named with the environment
variable that fixes it.

### 4. Seed subjects and produce one

```bash
docker compose exec app python -m echoes.cli topics --count 8
docker compose exec app python -m echoes.cli produce
```

Watch it. The finished file lands in
`/app/data/work/job-<id>/render/echoes-<id>.mp4` inside the container, on the
`app-data` volume.

---

## B. Railway

### 1. Create the services

- A **PostgreSQL** database (Railway's plugin).
- A service from this repository. `railway.json` selects the Dockerfile.

### 2. Attach a volume — this one matters

Mount a volume at **`/app/data`**.

Without it the container filesystem is discarded on every redeploy, which
means a render interrupted by a deploy restarts from nothing, and finished
files vanish before they are uploaded. `doctor` reports
`storage_warning` when it cannot detect a volume; treat that as an error.

If you would rather not pay for a volume, set `STORAGE_PROVIDER=s3` and point
it at Cloudflare R2 (10 GB free, no egress charge). Work-in-progress still
needs local scratch space, but finished media survives independently.

### 3. Set the variables

Everything from `.env.example`. `DATABASE_URL` comes from the Postgres plugin;
Railway injects `PORT` itself.

### 4. Turn on automatic deploys — check this explicitly

Service → **Settings** → **Source** → under *Branch connected to production*
the branch must be `main`, and the line below it must **not** read
*Auto deploy is disabled*. If it does, press **Enable**.

This is easy to miss and the symptom is misleading. With auto-deploy off,
pushing a fix to `main` does nothing, and the service keeps serving the old
build — so the bug you just fixed appears to survive the fix.

Two things that look like a fix and are not:

- **Redeploy** re-runs the *same* commit that is already deployed. It does not
  fetch anything new from GitHub.
- **Enabling** auto-deploy does not deploy commits that were pushed while it
  was off. It applies from the next push onward.

To confirm which code is live, open **Deployments**: each entry shows the
commit message it was built from. Compare it with the latest commit on `main`.

### 5. Health check

`railway.json` already points the health check at `/healthz` with a 300-second
start period. `/healthz` deliberately still answers when startup has failed,
reporting the reason — a service that goes dark on a bad configuration tells
you nothing.

---

## Connecting n8n

A workflow has already been created on your n8n instance:

**“Echoes of History — daily production”** — 13 nodes, two schedule triggers
and a manual one, timezone `Asia/Baghdad`.

It holds no secrets. The API token lives in an n8n credential.

### 1. Point it at your backend

Open the **Config** node and set `baseUrl`:

- Docker Compose: `http://app:8080` (the default — n8n reaches the app over
  the compose network, so the app's port never needs to be public)
- Railway: your service's public URL

Do the same in **Config (reconcile)**.

### 2. Create the credential

On **Start production**, create a new *Custom Auth (templated)* credential
named `Echoes API token` with:

```json
{"headers": {"Authorization": "Bearer {{api_key}}"}}
```

and set `api_key` to your `API_TOKEN`. Reuse the same credential on
**Reconcile uploads in doubt**.

### 3. Test before activating

Run **Run one now** manually. Trace it:

- `Read pause flags` → 200 with `scheduler_paused: false`
- `Not paused or stopped?` → true branch
- `Read status` → `database: "up"`, `counters.running: 0`
- `Nothing already running?` → true branch
- `Start production` → `{"accepted": true}`

Then activate the workflow. The daily trigger fires at 03:00 Baghdad time;
the hourly one resolves any upload whose outcome is unknown.

The idempotency key is derived from the date, so a retried firing on the same
day resolves to the same job rather than producing a second documentary.

---

## Going live

Do these in order. Each one is reversible; the last is not.

1. **Watch a dry run end to end.** Actually watch the video. The pipeline can
   be working perfectly and the documentary still be dull.

2. **Confirm your Google Cloud project is OAuth-verified.** Unverified apps
   can upload but the video stays locked private. Verification is Google's
   process and takes time; start it early.

3. **Get a refresh token** with the `youtube.upload` scope, and set
   `YOUTUBE_CLIENT_ID`, `YOUTUBE_CLIENT_SECRET`, `YOUTUBE_REFRESH_TOKEN`.

4. **Set `DRY_RUN=false` with `PUBLISH_MODE=private`.** Now it really uploads,
   but only you can see it. Check the video, the thumbnail, the description,
   and that the chapter markers appear.

5. **Only then widen** to `unlisted`, `scheduled` or `public`.

6. **Turn on the scheduler** (`SCHEDULER_ENABLED=true`, activate the n8n
   workflow).

If a refresh token is revoked the system says so by name (`invalid_grant`)
and stops rather than retrying — re-authorising is a human step and no amount
of retrying substitutes for it.

---

## Required accounts

| Service | Needed for | Cost |
|---|---|---|
| Google Cloud + YouTube Data API | Uploading | Free; **verification required for public videos** |
| Google AI Studio (Gemini) | Script, fact-check, metadata | Free tier, then a few pence per documentary |
| PostgreSQL | All durable state | Free self-hosted |
| Telegram bot *(optional)* | Notifications | Free |
| Cloudflare R2 *(optional)* | Durable media storage | Free to 10 GB |
| Railway *(optional)* | Hosting | No free service tier |

No account is needed for research or imagery. Wikipedia, the Library of
Congress, the Met and Crossref are all keyless.
