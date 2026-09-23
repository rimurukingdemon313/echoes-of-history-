-- Initial schema for the Echoes of History production system.
--
-- Two ideas shape it:
--
-- 1. A documentary is produced by a *job* that walks a fixed list of stages.
--    Stage results live in their own tables and are keyed by job, so a crash
--    between two stages loses nothing: the runner reads what already exists
--    and starts at the first stage that has no row.
--
-- 2. Every factual sentence that reaches narration is traceable. A claim
--    points at the script it came from and, through claim_sources, at the
--    sources that support it. An unsupported claim is therefore a query, not
--    an opinion.

CREATE TABLE IF NOT EXISTS settings (
    key           TEXT PRIMARY KEY,
    value         JSONB NOT NULL,
    updated_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- topics --
CREATE TABLE IF NOT EXISTS topics (
    id                BIGSERIAL PRIMARY KEY,
    title             TEXT NOT NULL,
    -- Lowercased, punctuation and stop-words stripped. The uniqueness of a
    -- topic is decided on this, never on the display title, so "The Siege of
    -- Constantinople" and "Siege of Constantinople, The" collide as they
    -- should.
    normalized_title  TEXT NOT NULL UNIQUE,
    subject           TEXT,
    period            TEXT,
    region            TEXT,
    category          TEXT,
    angle             TEXT,
    status            TEXT NOT NULL DEFAULT 'IDEA',
    priority          INTEGER NOT NULL DEFAULT 100,
    rejection_reason  TEXT,
    youtube_url       TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    published_at      TIMESTAMPTZ,
    CONSTRAINT topics_status_valid CHECK (status IN (
        'IDEA','RESEARCHING','RESEARCHED','SCRIPTING','FACT_CHECK',
        'READY_FOR_PRODUCTION','PRODUCING','READY_FOR_UPLOAD','UPLOADED',
        'PUBLISHED','FAILED','RETRY','REJECTED'
    ))
);
CREATE INDEX IF NOT EXISTS topics_status_idx ON topics (status, priority, created_at);

-- Token set of the normalized title, for cheap similarity pre-filtering
-- before the more expensive comparison runs.
CREATE TABLE IF NOT EXISTS topic_tokens (
    topic_id   BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    token      TEXT NOT NULL,
    PRIMARY KEY (topic_id, token)
);
CREATE INDEX IF NOT EXISTS topic_tokens_token_idx ON topic_tokens (token);

-- ------------------------------------------------------------------ jobs --
CREATE TABLE IF NOT EXISTS jobs (
    id                BIGSERIAL PRIMARY KEY,
    topic_id          BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    -- Supplied by the caller (n8n, the scheduler, a manual trigger). Two
    -- requests carrying the same key are the same job, which is what stops a
    -- retried webhook from starting a second production run.
    idempotency_key   TEXT NOT NULL UNIQUE,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    current_stage     TEXT,
    dry_run           BOOLEAN NOT NULL DEFAULT TRUE,
    publish_mode      TEXT NOT NULL DEFAULT 'private',
    version_stamp     TEXT NOT NULL,
    started_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at       TIMESTAMPTZ,
    error             TEXT,
    CONSTRAINT jobs_status_valid CHECK (status IN
        ('PENDING','RUNNING','SUCCEEDED','FAILED','CANCELLED','NEEDS_ATTENTION'))
);
CREATE INDEX IF NOT EXISTS jobs_status_idx ON jobs (status, started_at DESC);
CREATE INDEX IF NOT EXISTS jobs_topic_idx ON jobs (topic_id);

CREATE TABLE IF NOT EXISTS stage_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    stage         TEXT NOT NULL,
    status        TEXT NOT NULL DEFAULT 'RUNNING',
    attempt       INTEGER NOT NULL DEFAULT 1,
    started_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at   TIMESTAMPTZ,
    duration_ms   BIGINT,
    error         TEXT,
    output        JSONB,
    CONSTRAINT stage_runs_status_valid CHECK (status IN
        ('RUNNING','SUCCEEDED','FAILED','SKIPPED'))
);
-- One *successful* run per stage per job. The partial index is what makes
-- resumption safe: re-running a job cannot produce a second success row, so
-- "has this stage completed" is a primary-key-speed lookup.
CREATE UNIQUE INDEX IF NOT EXISTS stage_runs_one_success
    ON stage_runs (job_id, stage) WHERE status = 'SUCCEEDED';
CREATE INDEX IF NOT EXISTS stage_runs_job_idx ON stage_runs (job_id, started_at);

-- -------------------------------------------------------------- research --
CREATE TABLE IF NOT EXISTS sources (
    id             BIGSERIAL PRIMARY KEY,
    url            TEXT NOT NULL,
    title          TEXT,
    provider       TEXT NOT NULL,
    source_type    TEXT,
    author         TEXT,
    published      TEXT,
    licence        TEXT,
    retrieved_at   TIMESTAMPTZ NOT NULL DEFAULT now(),
    -- 0..1. Institutional archives and peer-reviewed work rank above
    -- general reference; the scorer records why in `rationale`.
    quality        REAL NOT NULL DEFAULT 0.5,
    rationale      TEXT,
    content_hash   TEXT,
    UNIQUE (url)
);

CREATE TABLE IF NOT EXISTS research_packages (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    topic_id      BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    summary       TEXT,
    source_count  INTEGER NOT NULL DEFAULT 0,
    conflicts     JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id)
);

CREATE TABLE IF NOT EXISTS research_facts (
    id            BIGSERIAL PRIMARY KEY,
    package_id    BIGINT NOT NULL REFERENCES research_packages(id) ON DELETE CASCADE,
    source_id     BIGINT REFERENCES sources(id) ON DELETE SET NULL,
    statement     TEXT NOT NULL,
    excerpt       TEXT,
    -- 'established' | 'interpretation' | 'disputed' | 'uncertain'
    confidence    TEXT NOT NULL DEFAULT 'uncertain',
    entities      JSONB NOT NULL DEFAULT '[]'::jsonb,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS research_facts_pkg_idx ON research_facts (package_id);

-- --------------------------------------------------------------- scripts --
CREATE TABLE IF NOT EXISTS scripts (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    topic_id        BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    version         INTEGER NOT NULL DEFAULT 1,
    word_count      INTEGER NOT NULL DEFAULT 0,
    estimated_s     REAL NOT NULL DEFAULT 0,
    status          TEXT NOT NULL DEFAULT 'DRAFT',
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, version)
);

CREATE TABLE IF NOT EXISTS script_chapters (
    id            BIGSERIAL PRIMARY KEY,
    script_id     BIGINT NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
    ordinal       INTEGER NOT NULL,
    heading       TEXT NOT NULL,
    body          TEXT NOT NULL,
    word_count    INTEGER NOT NULL DEFAULT 0,
    -- Filled in by the narration stage from measured audio, not estimated.
    start_s       REAL,
    duration_s    REAL,
    UNIQUE (script_id, ordinal)
);

CREATE TABLE IF NOT EXISTS claims (
    id            BIGSERIAL PRIMARY KEY,
    script_id     BIGINT NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
    chapter_id    BIGINT REFERENCES script_chapters(id) ON DELETE CASCADE,
    text          TEXT NOT NULL,
    kind          TEXT,
    -- 'supported' | 'unsupported' | 'contradicted' | 'uncertain'
    verdict       TEXT NOT NULL DEFAULT 'uncertain',
    note          TEXT,
    resolved      BOOLEAN NOT NULL DEFAULT FALSE,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS claims_script_idx ON claims (script_id, verdict);

CREATE TABLE IF NOT EXISTS claim_sources (
    claim_id   BIGINT NOT NULL REFERENCES claims(id) ON DELETE CASCADE,
    source_id  BIGINT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
    strength   REAL NOT NULL DEFAULT 0.5,
    PRIMARY KEY (claim_id, source_id)
);

-- ----------------------------------------------------------------- media --
CREATE TABLE IF NOT EXISTS audio_jobs (
    id             BIGSERIAL PRIMARY KEY,
    job_id         BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    script_id      BIGINT NOT NULL REFERENCES scripts(id) ON DELETE CASCADE,
    provider       TEXT NOT NULL,
    voice          TEXT NOT NULL,
    chunk_total    INTEGER NOT NULL DEFAULT 0,
    chunk_done     INTEGER NOT NULL DEFAULT 0,
    duration_s     REAL,
    storage_key    TEXT,
    status         TEXT NOT NULL DEFAULT 'PENDING',
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id)
);

-- One row per synthesised chunk. Survives a crash so a resumed narration
-- re-synthesises only what is missing rather than the whole documentary.
CREATE TABLE IF NOT EXISTS audio_chunks (
    id             BIGSERIAL PRIMARY KEY,
    audio_job_id   BIGINT NOT NULL REFERENCES audio_jobs(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    chapter_id     BIGINT REFERENCES script_chapters(id) ON DELETE SET NULL,
    text_hash      TEXT NOT NULL,
    path           TEXT,
    duration_s     REAL,
    status         TEXT NOT NULL DEFAULT 'PENDING',
    attempts       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (audio_job_id, ordinal)
);

CREATE TABLE IF NOT EXISTS visual_assets (
    id             BIGSERIAL PRIMARY KEY,
    job_id         BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    chapter_id     BIGINT REFERENCES script_chapters(id) ON DELETE SET NULL,
    ordinal        INTEGER NOT NULL,
    provider       TEXT NOT NULL,
    source_url     TEXT,
    -- Never null in practice: the visual stage refuses an asset whose licence
    -- it cannot name, because an unlicensed image is a copyright strike.
    licence        TEXT NOT NULL,
    attribution    TEXT,
    creator        TEXT,
    local_path     TEXT,
    plate_path     TEXT,
    start_s        REAL,
    duration_s     REAL,
    motion         TEXT,
    perceptual_hash TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id, ordinal)
);
CREATE INDEX IF NOT EXISTS visual_assets_job_idx ON visual_assets (job_id, start_s);

CREATE TABLE IF NOT EXISTS render_jobs (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    status          TEXT NOT NULL DEFAULT 'PENDING',
    segment_total   INTEGER NOT NULL DEFAULT 0,
    segment_done    INTEGER NOT NULL DEFAULT 0,
    width           INTEGER NOT NULL,
    height          INTEGER NOT NULL,
    fps             INTEGER NOT NULL,
    duration_s      REAL,
    bytes           BIGINT,
    storage_key     TEXT,
    started_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    finished_at     TIMESTAMPTZ,
    error           TEXT,
    UNIQUE (job_id)
);

CREATE TABLE IF NOT EXISTS render_segments (
    id             BIGSERIAL PRIMARY KEY,
    render_job_id  BIGINT NOT NULL REFERENCES render_jobs(id) ON DELETE CASCADE,
    ordinal        INTEGER NOT NULL,
    asset_id       BIGINT REFERENCES visual_assets(id) ON DELETE SET NULL,
    path           TEXT,
    duration_s     REAL NOT NULL,
    status         TEXT NOT NULL DEFAULT 'PENDING',
    attempts       INTEGER NOT NULL DEFAULT 0,
    UNIQUE (render_job_id, ordinal)
);

-- ---------------------------------------------------------------- output --
CREATE TABLE IF NOT EXISTS videos (
    id              BIGSERIAL PRIMARY KEY,
    job_id          BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    topic_id        BIGINT NOT NULL REFERENCES topics(id) ON DELETE CASCADE,
    title           TEXT NOT NULL,
    description     TEXT NOT NULL,
    tags            JSONB NOT NULL DEFAULT '[]'::jsonb,
    chapters        JSONB NOT NULL DEFAULT '[]'::jsonb,
    duration_s      REAL,
    video_path      TEXT,
    thumbnail_path  TEXT,
    qc_report       JSONB,
    version_stamp   TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (job_id)
);

-- The upload record exists *before* the upload is attempted, carrying the
-- idempotency key. That ordering is the whole defence against a second copy
-- on the channel: a crash after upload but before the response is recorded
-- leaves a row saying an attempt was in flight, and recovery queries YouTube
-- instead of uploading again.
CREATE TABLE IF NOT EXISTS youtube_uploads (
    id                BIGSERIAL PRIMARY KEY,
    job_id            BIGINT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
    video_id          BIGINT NOT NULL REFERENCES videos(id) ON DELETE CASCADE,
    idempotency_key   TEXT NOT NULL UNIQUE,
    youtube_video_id  TEXT,
    upload_url        TEXT,
    bytes_sent        BIGINT NOT NULL DEFAULT 0,
    bytes_total       BIGINT,
    privacy_status    TEXT NOT NULL DEFAULT 'private',
    publish_at        TIMESTAMPTZ,
    status            TEXT NOT NULL DEFAULT 'PENDING',
    error             TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT now(),
    completed_at      TIMESTAMPTZ,
    CONSTRAINT uploads_status_valid CHECK (status IN
        ('PENDING','IN_FLIGHT','AMBIGUOUS','SUCCEEDED','FAILED','SKIPPED_DRY_RUN'))
);
CREATE UNIQUE INDEX IF NOT EXISTS youtube_uploads_video_unique
    ON youtube_uploads (youtube_video_id) WHERE youtube_video_id IS NOT NULL;

-- ------------------------------------------------------- observability ----
CREATE TABLE IF NOT EXISTS workflow_runs (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    source        TEXT NOT NULL,
    external_id   TEXT,
    payload       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS errors (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT REFERENCES jobs(id) ON DELETE CASCADE,
    stage         TEXT,
    kind          TEXT NOT NULL,
    message       TEXT NOT NULL,
    retryable     BOOLEAN NOT NULL DEFAULT FALSE,
    context       JSONB,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
CREATE INDEX IF NOT EXISTS errors_created_idx ON errors (created_at DESC);

CREATE TABLE IF NOT EXISTS notifications (
    id            BIGSERIAL PRIMARY KEY,
    job_id        BIGINT REFERENCES jobs(id) ON DELETE SET NULL,
    level         TEXT NOT NULL,
    event         TEXT NOT NULL,
    message       TEXT NOT NULL,
    delivered     BOOLEAN NOT NULL DEFAULT FALSE,
    provider      TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
