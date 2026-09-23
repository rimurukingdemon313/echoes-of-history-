# Echoes of History

An autonomous production system for long-form historical documentaries. It
chooses a subject, researches it against public archives, writes a
sixty-to-ninety-minute script, checks every factual claim back against its
sources, narrates it, assembles a picture track from licence-checked archive
imagery, renders it, and uploads it to YouTube — then records what it did and
moves on.

It is built to run unattended for weeks and to fail safely when it cannot.

---

## What it will and will not do

**It will** refuse to publish more often than you might expect. That is the
design. A documentary that cannot be supported by its sources, that comes out
too short, or that duplicates one already made, is abandoned rather than
shipped. `NO TRADE` has an analogue here: **no video is the expected result**
whenever the evidence does not support one.

**It will not** promise that its output is accurate enough to publish
unsupervised on day one. Watch the first several end to end.

---

## How it works

```
scheduler (n8n)
      │
      ▼
topic engine ──► originality guard ──► research ──► fact extraction
                                                          │
                                                          ▼
                                      script engine ──► fact-check ──► revision
                                                          │
                                                          ▼
                              narration (Piper) ──► measured chapter timings
                                                          │
                                                          ▼
                                    visual plan ──► licence-checked imagery
                                                          │
                                                          ▼
                    segment render ──► concat (-c copy) ──► mux with audio
                                                          │
                                                          ▼
                          thumbnail ──► metadata ──► QUALITY CONTROL
                                                          │
                                                   (pass) │ (fail: stop)
                                                          ▼
                                       YouTube upload ──► record ──► notify
```

Eleven stages, each one a row in the database. A crash resumes at the first
stage that has not succeeded — a failure after rendering costs a minute, not
an hour.

---

## The rules this system is built on

These are not style preferences. Each exists because violating it produces a
specific failure.

1. **Nothing publishes without passing quality control.** Twenty-eight checks
   run against the finished artefacts, not against what the pipeline believed
   it produced. The video file is probed; the script is re-measured.

2. **Every factual sentence is traceable to a source.** Claims are extracted
   from the finished script and checked against evidence *retrieved from the
   research package*, never against the model's own knowledge. A claim the
   package does not cover is `unsupported`, and too many unsupported claims
   refuse the documentary.

3. **A revision may only improve the script.** Rewrites are scored and kept
   per chapter only when they reduce the failing-claim count. Observed in
   testing: an unguarded revision turned 5 failures into 28.

4. **Writes are never blindly retried.** A YouTube upload is a write with no
   idempotency key. The resumable session URL is stored *before any bytes are
   sent*, so an interrupted upload is resolved by asking YouTube what it
   already holds. The system can lose its connection mid-upload and still
   never post a second copy.

5. **Licences are checked, never assumed.** An image whose licence cannot be
   recognised is discarded at the provider boundary. `CC BY-NC` is refused —
   it contains the substring `CC BY`, which is exactly the trap.

6. **Dry-run scaffolding cannot reach a real channel.** Offline-written
   scripts, synthetic plates and fixture sources are each a *blocking* quality
   failure whenever `DRY_RUN=false`.

7. **Controls that reduce activity are never locked.** Pause and stop need no
   token and work from any device. Anything that starts, resumes or widens
   activity requires one — and with no token configured those are refused
   rather than left open.

8. **The system measures itself.** The narration's real speaking rate is
   measured and fed back, so the next script is sized from evidence rather
   than from a configured guess.

---

## Quick start (self-hosted, free)

```bash
cp .env.example .env
# set POSTGRES_PASSWORD and API_TOKEN at minimum
docker compose up -d --build
docker compose exec app python -m echoes.cli doctor
```

Then open `http://localhost:8080` for the dashboard and
`http://localhost:5678` for n8n.

### Produce one documentary by hand

```bash
docker compose exec app python -m echoes.cli produce
```

With `DRY_RUN=true` this runs everything up to the upload and stops, leaving
the finished file under `/app/data/work/job-<id>/render/`.

---

## Commands

| Command | What it does |
|---|---|
| `python -m echoes.cli doctor` | Checks config, ffmpeg, database, providers, voice model, storage durability |
| `python -m echoes.cli migrate` | Applies pending migrations (forward-only) |
| `python -m echoes.cli status` | Current state as JSON |
| `python -m echoes.cli topics --count 8` | Adds candidate subjects, rejecting duplicates |
| `python -m echoes.cli produce` | Produces one documentary |
| `python -m echoes.cli produce --live` | Same, with `DRY_RUN` off for this run |
| `python -m echoes.cli resume <job_id>` | Continues a job from its first incomplete stage |
| `python -m echoes.cli reconcile` | Resolves uploads whose outcome is unknown |

---

## HTTP API

Everything n8n drives. Split by **direction**, not by endpoint.

**No token required — these can only reduce activity:**

| Endpoint | Purpose |
|---|---|
| `GET /healthz` | Liveness; reports startup errors rather than hiding them |
| `GET /status` | Full system state |
| `GET /jobs`, `GET /jobs/{id}` | Job history, stage timings, QC report |
| `GET /paused` | Current pause/stop flags |
| `POST /scheduler/pause` | Stop the scheduler |
| `POST /stop` | Refuse all new production |
| `GET /` | Dashboard |

**Token required (`Authorization: Bearer <API_TOKEN>` or `X-API-Token`):**

| Endpoint | Purpose |
|---|---|
| `POST /produce` | Start one documentary (returns immediately) |
| `POST /jobs/{id}/resume` | Resume a job |
| `POST /topics/replenish` | Add candidate subjects |
| `POST /uploads/reconcile` | Resolve uploads in doubt |
| `POST /scheduler/resume` | Un-pause |

---

## Swapping providers

Every external dependency sits behind a small protocol. Changing one is a
settings value plus one class — never a change to the pipeline.

| Capability | Setting | Shipped | To add another |
|---|---|---|---|
| Language model | `LLM_PROVIDER` | `gemini`, `offline` | Implement `generate` / `generate_json` / `available` in `echoes/providers/llm/`, register in `registry._llm` |
| Narration | `TTS_PROVIDER` | `piper`, `silent` | Implement `synthesize(text, path) -> seconds` in `echoes/providers/tts/` |
| Research | `RESEARCH_PROVIDERS` | `wikipedia`, `loc`, `met`, `crossref`, `fixtures` | Implement `search(query, limit) -> [SourceDoc]` |
| Imagery | `IMAGE_PROVIDERS` | `wikimedia`, `met`, `loc`, `synthetic` | Implement `search(query, limit) -> [ImageCandidate]` |
| Storage | `STORAGE_PROVIDER` | `local`, `s3` | Implement `put` / `get` / `exists` / `delete` / `durable` |
| Notifications | `NOTIFY_PROVIDER` | `noop`, `telegram` | Implement `send(level, event, message) -> bool` |

The registry **refuses an unknown name** rather than falling back to a
default — a silent substitution would make every recorded `provider` value a
claim about code that did not run.

---

## Operating it

### Change the publishing mode

`PUBLISH_MODE` is one of `private`, `unlisted`, `public`, `scheduled`.
`DRY_RUN=true` overrides all of them — nothing uploads.

`scheduled` uploads as **private with a `publishAt` timestamp**, at
`PUBLISH_HOUR_LOCAL` in `TZ`. Sending `public` with a future `publishAt` does
not schedule a video; it publishes it immediately. The upload client refuses
that combination rather than letting it happen.

### Stop the automation

```bash
curl -X POST http://localhost:8080/stop            # no new production
curl -X POST http://localhost:8080/scheduler/pause # stop the schedule
```

Or the two buttons on the dashboard. Neither needs a token. A production
already running finishes its current stage and stops at the next checkpoint.

### Back up the database

```bash
docker compose exec -T db pg_dump -U echoes echoes | gzip > echoes-$(date +%F).sql.gz
```

Restore:

```bash
gunzip -c echoes-2026-09-23.sql.gz | docker compose exec -T db psql -U echoes -d echoes
```

The database is the system's memory: which subjects are taken, which stages
completed, which video ids were uploaded. The media files can be regenerated;
this cannot.

### Recover from a crash

```bash
python -m echoes.cli status                # what was running
python -m echoes.cli reconcile             # resolve uploads in doubt FIRST
python -m echoes.cli resume <job_id>       # continue from the first gap
```

Always reconcile before resuming. If a job died mid-upload, reconciliation
establishes whether the video already exists; resuming first would be the one
path that could produce a duplicate.

---

## Cost

Under the default configuration, per documentary:

| Component | Choice | Cost |
|---|---|---|
| Narration | Piper, local | £0 — no API, no per-character charge, no rate limit |
| Research | Wikipedia, Library of Congress, Met, Crossref | £0 — all keyless |
| Imagery | Wikimedia Commons, Met Open Access, LoC | £0 — public domain and CC |
| Rendering | FFmpeg | £0 |
| Database | PostgreSQL | £0 self-hosted |
| Script and fact-check | Gemini | Free tier covers roughly one documentary a day at current limits; beyond that, a few pence per film at Flash pricing |
| Hosting | Your own machine, or Railway | £0 self-hosted; Railway has no free service tier |
| Storage | Local volume, or Cloudflare R2 | £0 up to R2's 10 GB free tier |

The only unavoidable spend is hosting if you do not self-host, and Gemini
beyond its free tier. Everything else is free by construction, not by
squeezing a trial.

---

## Known limitations

Stated plainly, because finding these yourself would waste your time.

1. **Live provider calls are unverified in this build.** The machine this was
   developed on blocks outbound access to Wikipedia, the Library of Congress,
   the Met, Crossref, Telegram and Hugging Face. Those providers are written
   against their documented APIs and are covered by offline tests, but the
   first real run is their first real exercise. `doctor` will tell you
   quickly.

2. **The Docker image is unbuilt here** for the same reason: the build
   downloads the Piper voice from Hugging Face. Everything inside it has been
   run directly.

3. **Public uploads need a verified Google Cloud project.** An unverified
   OAuth app can upload, but the video stays locked private regardless of
   `PUBLISH_MODE`. This is Google's rule, not a limitation of this code.

4. **Rendering is the slow part.** Measured on 4 cores: roughly 1.4–3×
   real time, so a 90-minute documentary takes 30–60 minutes to encode.
   Budget for it; it is why the scheduler defaults to 03:00.

5. **Conflict detection only sees figures.** Sources disagreeing in prose
   rather than in numbers are caught by the fact-checker, if at all, not by
   the conflict detector.

6. **Evidence retrieval is lexical.** A claim paraphrased with entirely
   different vocabulary may retrieve nothing and be marked `unsupported`.
   That is the safe direction to fail, but it means the unsupported ratio
   is a conservative estimate rather than a precise one.

7. **Music is the weakest automated link.** The system mixes and ducks a bed
   correctly, but it has no licensed library of its own. Drop your own
   CC0/CC-BY tracks into `assets/music/`; with none present it renders
   narration alone, which is correct for a sleep-history channel anyway.

8. **The offline provider is a simulator, not a writer.** It exists so the
   pipeline can be exercised at full scale without a key. Its prose is
   assembled from research facts plus fixed connectives, and quality control
   refuses to let it near a real channel.

---

## Where everything is

| You want | Look at |
|---|---|
| Deployment steps, both paths | `docs/DEPLOYMENT.md` |
| What to do when it breaks | `docs/RUNBOOK.md` |
| Why it is shaped this way | `docs/ARCHITECTURE.md` |
| Secrets, the auth model, leak response | `docs/SECURITY.md` |
| Every setting, with commentary | `.env.example` |
| Database schema | `echoes/db/migrations/` |
| The n8n workflow | Already on your instance: *Echoes of History — daily production* |

---

## Development

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements-dev.txt
export ECHOES_TEST_DATABASE_URL=postgresql://...   # for the database tests
.venv/bin/python -m pytest
```

144 tests. They assert behaviour, not shape: scenarios are constructed so the
correct answer is known, the clock is pinned, and the encoder is real ffmpeg
rather than a mock. Several of them found real bugs in this code and the code
changed.

Bump the matching constant in `echoes/version.py` whenever you change what a
component *does*. Every video records the stamp, and performance is grouped by
it; a silent behaviour change makes past results uninterpretable.
