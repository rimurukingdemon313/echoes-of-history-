# Runbook

What to do when something has gone wrong, ordered by what you will actually
see first.

---

## The dashboard says a job is stuck on a stage

Stages are slow, not stuck. Typical durations at 90 minutes:

| Stage | Typical |
|---|---|
| research | 10–60 s |
| script | 1–5 min |
| factcheck | 1–4 min |
| narration | 2–5 min |
| visuals | 1–10 min (downloads) |
| **render** | **30–60 min** |
| thumbnail / metadata / qc | seconds |
| upload | 5–30 min, depending on the link |

Render is the long one. Check it is moving:

```bash
ls /app/data/work/job-<id>/render/segments/*.mp4 | wc -l
```

If that number is rising, leave it alone.

---

## A job failed

```bash
python -m echoes.cli status
curl -s localhost:8080/jobs/<id> | python3 -m json.tool
```

The stage history carries the error. Then:

```bash
python -m echoes.cli resume <id>
```

Resuming re-runs only the failed stage onwards. Completed stages are skipped,
and completed render segments are reused.

---

## A job is `NEEDS_ATTENTION`

This means an **ambiguous outcome**: a write whose result is unknown, almost
always an upload whose connection dropped. The system deliberately did not
retry, because retrying could put a second copy on the channel.

```bash
python -m echoes.cli reconcile
```

This asks YouTube what it already holds. It never uploads. Possible answers:

- `was_complete` — the video exists; the record has been corrected. Done.
- `resumable` — YouTube holds part of the file. `resume` the job to finish it.
- `expired` — the session is gone (they last about a week). Resume the job;
  it will start a fresh upload.

**Always reconcile before resuming a job that died during upload.**

---

## Quality control refused a documentary

This is the system working. The report names every failed check:

```bash
curl -s localhost:8080/jobs/<id> | python3 -c "import json,sys; print(json.dumps(json.load(sys.stdin)['qc'], indent=2))"
```

| Check | Meaning | What to do |
|---|---|---|
| `factcheck_within_limit` | Too many claims unsupported | Widen `RESEARCH_PROVIDERS`, or accept the topic is too obscure |
| `narration_duration_in_range` | Too short or too long | Usually too few sources; the script engine will not pad |
| `sources_present` | Below `MIN_SOURCES_PER_DOCUMENTARY` | Pick another subject |
| `visuals_licensed` | An asset had no readable licence | A provider bug; the asset should have been dropped earlier |
| `script_not_synthetic` | Offline simulator output on a live run | `LLM_PROVIDER` is `offline` but `DRY_RUN` is false |
| `visuals_not_synthetic` | Placeholder plates on a live run | `IMAGE_PROVIDERS` still `synthetic` |
| `sources_not_fixtures` | Offline sources on a live run | `RESEARCH_PROVIDERS` still `fixtures` |
| `video_not_empty` | File too small for its length | A broken render; delete the segments and resume |
| `chapters_valid` | Markers YouTube would reject | Usually a timing bug; check narration ran |

The last three `*_not_*` checks all mean the same thing: something is still
configured for a dry run. `doctor` will show which.

---

## Nothing has produced for days

```bash
curl -s localhost:8080/paused
```

If `stop_requested` or `scheduler_paused` is true, someone pressed the button:

```bash
curl -X POST -H "Authorization: Bearer $API_TOKEN" localhost:8080/scheduler/resume
```

`stop_requested` is cleared the same way. If both are false, check the n8n
workflow is **active** and look at its execution history.

---

## Every topic is being rejected as a duplicate

The pool is exhausted against the originality threshold.

```bash
python -m echoes.cli topics --count 20
```

If that still rejects everything, set `PREFERRED_ERAS` to open new ground:

```
PREFERRED_ERAS=Mesoamerica,Central Asia,maritime Southeast Asia,pre-colonial West Africa
```

Lowering `SIMILARITY_THRESHOLD` is the wrong fix. It does not create new
subjects; it just lets near-duplicates through.

---

## The database is down

The system refuses to start new work and says so. This is deliberate: a
production it cannot record is one it may silently repeat.

Existing jobs stop at their next checkpoint. Once the database is back,
`resume` each one. Nothing is lost that was already written.

---

## Disk is filling up

Segments are the culprit — roughly the size of the finished film again. They
are cleaned automatically once a job is recorded, but a run of failures
leaves them behind.

```bash
du -sh /app/data/work/*
rm -rf /app/data/work/job-<id>        # only for jobs already recorded or abandoned
```

Never delete the work directory of a job you intend to resume: that is what
makes resumption cheap.

---

## Authentication expired

The log says `invalid_grant` and the job stops. A refresh token has been
revoked, or the OAuth consent screen was changed.

Re-authorise the channel, set the new `YOUTUBE_REFRESH_TOKEN`, restart, and
resume the job. The system will not retry around this, because retrying a
dead credential looks exactly like the service being down and would hide the
real problem.
