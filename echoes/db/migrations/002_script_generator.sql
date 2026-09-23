-- Record which provider wrote a script, as a column rather than a marker in
-- the prose.
--
-- The offline simulator's output must never reach a real channel, and the
-- first implementation flagged it by appending a marker string to the last
-- chapter's body. That body is the narration script: the marker would have
-- been read aloud by the voice. Quality control now reads this column
-- instead, and the prose stays prose.

ALTER TABLE scripts ADD COLUMN IF NOT EXISTS generator TEXT;

-- Existing rows predate the column; 'unknown' is honest and, because quality
-- control only blocks on a generator it recognises as synthetic, it does not
-- retroactively condemn anything already produced.
UPDATE scripts SET generator = 'unknown' WHERE generator IS NULL;
