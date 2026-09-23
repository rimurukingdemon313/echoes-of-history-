-- Separate "where the file is right now" from "where it will still be after
-- a redeploy".
--
-- Both columns were previously called storage_key and both held a local
-- path. For a local-volume deployment that happened to be true. For an S3
-- deployment it was not: the finished video was uploaded to object storage
-- and the returned key was thrown away, so nothing could find it again after
-- the container was replaced -- which is the exact situation object storage
-- was chosen to survive.

ALTER TABLE render_jobs ADD COLUMN IF NOT EXISTS local_path TEXT;
ALTER TABLE audio_jobs  ADD COLUMN IF NOT EXISTS local_path TEXT;

-- Existing rows put a local path in storage_key; move it to the column that
-- now means that, and leave storage_key null rather than claim a durable
-- copy exists when none was recorded.
UPDATE render_jobs SET local_path = storage_key WHERE local_path IS NULL;
UPDATE audio_jobs  SET local_path = storage_key WHERE local_path IS NULL;
UPDATE render_jobs SET storage_key = NULL
 WHERE storage_key IS NOT NULL AND storage_key LIKE '/%';
UPDATE audio_jobs  SET storage_key = NULL
 WHERE storage_key IS NOT NULL AND storage_key LIKE '/%';
