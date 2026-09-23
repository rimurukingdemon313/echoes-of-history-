# Architecture

## Why it is shaped this way

The system exists to run unattended. Every structural decision below follows
from that, and from one observation: the expensive part of making a
documentary is not thinking, it is encoding. A 90-minute film takes 30–60
minutes to render. Any design that loses that work to a restart is not
autonomous, it is merely automatic.

So the pipeline is a sequence of stages, and **a stage is a row**.

```
echoes/
├── config.py          Settings, read once, immutable. No secret has a default.
├── clock.py           The only source of time. Tests pin it.
├── errors.py          Retryable / Permanent / AmbiguousOutcome — the taxonomy
│                      the runner branches on.
├── logging.py         One JSON object per line, with secret redaction applied
│                      to the formatted record.
├── version.py         Behaviour stamps. Every video records one.
├── orchestrator.py    Decides whether work may start, what to make, and how a
│                      job is identified.
├── cli.py             Everything the HTTP API can do, from a terminal.
│
├── db/
│   ├── pool.py        Pooled psycopg. healthy() never raises.
│   ├── migrate.py     Forward-only. An edited applied migration is reported.
│   ├── migrations/    SQL. The partial unique index lives here.
│   └── repo.py        Data access shaped around the pipeline's questions.
│
├── providers/         Everything external, behind a protocol.
│   ├── http.py        Reads retry with jittered backoff. Writes do not —
│   │                  enforced by separate functions, not a flag.
│   ├── llm/           gemini, offline (a simulator, marked as such)
│   ├── tts/           piper, silent, calibrate
│   ├── research/      wikipedia, loc, met, crossref, fixtures
│   ├── images/        wikimedia, met, loc, synthetic — all licence-gated
│   ├── storage/       local (durability detected), s3
│   ├── notify/        noop, telegram
│   └── registry.py    Assembles from settings. Refuses unknown names.
│
├── media/ffmpeg.py    Every measured encoder decision, in one place.
│
├── pipeline/
│   ├── runner.py      Walks stages, skips completed, branches on error class.
│   ├── stages.py      The eleven stages, wired to the database.
│   ├── topics.py      Originality: lexical guard + semantic backstop.
│   ├── research.py    Sources → facts → conflicts. The model reads, never knows.
│   ├── script.py      Word budget, chapter outline, fact distribution.
│   ├── factcheck.py   Extract → retrieve → judge → revise → re-judge.
│   ├── narration.py   Chunked, resumable, measured.
│   ├── visuals.py     Timeline tiled exactly to the measured audio.
│   ├── render.py      Parallel segments, resumable, concat without re-encode.
│   ├── thumbnail.py   Concepts scored on measured text contrast.
│   ├── metadata.py    YouTube's limits, enforced here rather than discovered.
│   ├── qc.py          28 checks against the artefacts themselves.
│   └── upload.py      Resumable protocol as the duplicate defence.
│
└── api/               FastAPI surface + self-contained dashboard.
```

---

## The three decisions worth explaining

### 1. Stages read from the database, not from each other

A stage restarted in a fresh process must find the same inputs the original
had. Return values are small summaries for the log and the dashboard; they
are never how data moves. This is what makes `resume` work across a container
restart rather than only within one process.

The partial unique index is the enforcement:

```sql
CREATE UNIQUE INDEX stage_runs_one_success
    ON stage_runs (job_id, stage) WHERE status = 'SUCCEEDED';
```

A stage physically cannot succeed twice. "Has this completed" is a
primary-key-speed lookup, not a scan with a judgement call.

### 2. The error class decides the recovery, not the call site

```
Retryable        → backoff with full jitter, bounded by RETRY_MAX_ATTEMPTS
Permanent        → stop now; retrying burns quota to reach the same answer
AmbiguousOutcome → stop and flag for a human; NEVER retry
```

`AmbiguousOutcome` is the interesting one. It means a side effect may or may
not have happened. Retrying is the one action guaranteed to make things
worse. The recovery is to go and look at the remote state — which for uploads
is exactly what the resumable protocol offers.

### 3. The research package is the only source of fact

The model is used to *read* sources and to *judge* claims against retrieved
evidence. It is never asked what happened in 1453.

```
sources ──► facts (each pointing at its source row)
                        │
            script written from facts
                        │
            claims extracted from the finished script
                        │
      evidence retrieved from the package, lexically
                        │
        model judges claim against that evidence only
```

The retrieval step is what makes this more than a model marking its own
homework: the model never decides what evidence exists. A claim the package
does not cover is `unsupported`, which is the correct and safe answer.

---

## Data model

The tables that carry the guarantees:

| Table | Guarantee |
|---|---|
| `topics.normalized_title UNIQUE` | One documentary per subject, ever |
| `jobs.idempotency_key UNIQUE` | The same request twice is the same job |
| `stage_runs` partial unique index | A stage cannot succeed twice |
| `audio_chunks (audio_job_id, ordinal)` | Narration resumes per chunk |
| `render_segments (render_job_id, ordinal)` | Rendering resumes per segment |
| `youtube_uploads.idempotency_key UNIQUE` | One upload attempt per job |
| `youtube_uploads.youtube_video_id UNIQUE` | One database row per real video |
| `claims` → `claim_sources` | Every claim's support is a query, not an opinion |

---

## Timing, measured on 4 cores

| Stage | 60-minute documentary |
|---|---|
| research | ~1 s (offline fixtures) to ~60 s (live archives) |
| script | seconds to ~5 min depending on provider |
| factcheck | ~1 s to ~4 min |
| narration | ~110 s for 69 minutes of audio (~34x real time) |
| visuals | ~11 s (offline) to ~10 min (downloads) |
| **render** | **~25 min for 293 segments** |
| thumbnail, metadata, qc | < 2 s |

Render dominates, which is why segments are parallel, resumable, and encoded
exactly once.
