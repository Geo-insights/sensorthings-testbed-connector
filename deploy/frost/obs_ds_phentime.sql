-- OBS_DS_PHENTIME: a Datastream's newest Observation in one index probe.
--
-- FROST-Server does not create this index: its own indexes on "OBSERVATIONS" are
-- the primary key, "DATASTREAM_ID", "FEATURE_ID" and "OBS-DS_ID-ID"
-- ("DATASTREAM_ID", "ID"). Without it every "newest Observation of Datastream X"
-- reads and sorts all of X's rows, so the connector's restart seeding and the
-- platform's latest-value reads run into the 60 s statement_timeout.
--
-- Definition as on production (frost.geo-insights.nl, pg_indexes, 2026-11-20):
--   CREATE INDEX "OBS_DS_PHENTIME" ON public."OBSERVATIONS" USING btree ("DATASTREAM_ID", "PHENOMENON_TIME_START" DESC)
--
-- Idempotent. A valid index with this definition is left alone; an invalid one
-- (what an interrupted CONCURRENTLY build leaves behind) is dropped and built
-- again; an index of this name with another definition stops the script.
-- Exits 0 only when the index is valid at the end.
--
-- Run it with psql, never with -1/--single-transaction: CONCURRENTLY refuses to
-- run inside a transaction block. How and when: deploy/frost/README.md.

\set ON_ERROR_STOP on
\set AUTOCOMMIT on

-- Session settings only. The database default statement_timeout (60 s) would
-- cancel the build on a full table, and a lock_timeout would cancel it while it
-- waits for older transactions: either leaves an invalid index behind.
SET statement_timeout = 0;
SET lock_timeout = 0;

DO $$
DECLARE
    existing text := pg_get_indexdef(to_regclass('public."OBS_DS_PHENTIME"'));
BEGIN
    IF to_regclass('public."OBSERVATIONS"') IS NULL THEN
        RAISE EXCEPTION 'public."OBSERVATIONS" does not exist: start frost-server once so it creates its tables, then run this again';
    END IF;
    IF existing IS NOT NULL AND existing <> 'CREATE INDEX "OBS_DS_PHENTIME" ON public."OBSERVATIONS" USING btree ("DATASTREAM_ID", "PHENOMENON_TIME_START" DESC)' THEN
        RAISE EXCEPTION 'an index "OBS_DS_PHENTIME" exists with another definition: %. Check who made it before dropping it.', existing;
    END IF;
END
$$;

-- Drop the invalid leftover of an interrupted build (no rows, nothing to do).
SELECT 'DROP INDEX CONCURRENTLY IF EXISTS public."OBS_DS_PHENTIME"'
  FROM pg_index
 WHERE indexrelid = to_regclass('public."OBS_DS_PHENTIME"')
   AND NOT indisvalid
\gexec

-- Build it without blocking FROST's inserts. When it exists: a notice, no-op.
CREATE INDEX CONCURRENTLY IF NOT EXISTS "OBS_DS_PHENTIME"
    ON public."OBSERVATIONS" USING btree ("DATASTREAM_ID", "PHENOMENON_TIME_START" DESC);

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_index
                    WHERE indexrelid = to_regclass('public."OBS_DS_PHENTIME"') AND indisvalid) THEN
        RAISE EXCEPTION '"OBS_DS_PHENTIME" is missing or invalid after the build: run this file again';
    END IF;
END
$$;

SELECT c.relname AS index, i.indisvalid AS valid,
       pg_size_pretty(pg_relation_size(c.oid)) AS size, pg_get_indexdef(c.oid) AS definition
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
 WHERE i.indexrelid = to_regclass('public."OBS_DS_PHENTIME"');
