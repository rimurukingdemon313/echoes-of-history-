# Security

## What this system holds

A Gemini key, a YouTube refresh token that can upload to your channel, a
database URL with a password, and optionally a Telegram bot token and S3
credentials. The refresh token is the one that matters: it grants the ability
to publish under your name.

## Where secrets live

Environment variables, and nowhere else.

- **Never in the repository.** `.env` is git-ignored; `.env.example` carries
  placeholder names only.
- **Never in the n8n workflow.** The workflow JSON holds a *reference* to a
  credential, and the credential lives in n8n's own encrypted store. Exporting
  and sharing the workflow leaks nothing.
- **Never in logs.** `echoes/logging.py` redacts on the formatted record, so a
  key caught inside an exception message or an f-string is removed just as a
  key passed as a field would be. It matches both the configured values and
  the *shapes* of Google API keys, OAuth and refresh tokens, Telegram bot
  tokens, Postgres URLs and bearer headers.
- **Never in an error surfaced to a caller.** Gemini's 400 bodies can quote
  the key back when the key is the problem; those are truncated and passed
  through the same redaction.

## The authorization model

Split by **direction**, not by endpoint.

**Never locked** — these can only reduce activity:

```
POST /scheduler/pause
POST /stop
GET  /healthz  /status  /paused  /jobs  /
```

An operator must be able to stop the machine from any device, having lost
anything, including the token. A stop control behind a credential is a stop
control that fails exactly when it is needed.

**Token required** — these start, resume or widen activity:

```
POST /produce  /jobs/{id}/resume  /topics/replenish
POST /uploads/reconcile  /scheduler/resume
```

Accepted as `Authorization: Bearer <token>` or `X-API-Token: <token>`.

**With `API_TOKEN` unset, the second group returns 503 naming the variable.**
It is not left open. The failure this prevents is concrete: a deployment that
forgot one variable, reachable on the internet, with `/produce` unauthenticated.
`tests/test_api.py` drives all of this over real requests.

## Exposure

The application does not need to be public. Under the supplied
`docker-compose.yml`, n8n reaches it as `http://app:8080` on the compose
network, so the only port you need to publish is n8n's — and that has its own
authentication.

If you do publish it, put it behind TLS. `API_TOKEN` travels in a header.

A hostname is not a secret. Every certificate issued for one is published in
a public transparency log, so "nobody knows the URL" protects nothing.

## What the system will not do

- It will not retry a revoked credential. `invalid_grant` is reported as
  permanent and stops the job, because retrying a dead token is
  indistinguishable from the service being down and hides the real problem.
- It will not upload twice after an interrupted upload. It asks YouTube what
  it holds rather than sending again.
- It will not publish anything built from dry-run scaffolding, whatever
  `PUBLISH_MODE` says.

## Supply chain

Seven runtime dependencies, all widely used. The research and image providers
call documented public APIs over HTTPS with bounded timeouts, a size ceiling
on downloads, and no execution of anything retrieved. Downloaded images are
decoded by Pillow and re-encoded to a plate; the original bytes never reach
ffmpeg.

## If a credential leaks

1. Revoke it at the source first — Google Cloud console for the OAuth client,
   AI Studio for the Gemini key, BotFather for Telegram.
2. `POST /stop` so nothing new starts.
3. Rotate the variable and restart.
4. Check `youtube_uploads` and the channel for anything you did not expect.

Rotating `API_TOKEN` costs nothing: update the variable and the n8n
credential. Nothing else references it.
