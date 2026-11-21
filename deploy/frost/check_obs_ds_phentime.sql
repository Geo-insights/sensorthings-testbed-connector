SET default_transaction_read_only = on; SET statement_timeout = '50s';
-- Read-only check of OBS_DS_PHENTIME (obs_ds_phentime.sql builds it). Safe on
-- production at any time: it writes nothing and builds nothing.

-- Expect one row with valid = t and the definition in obs_ds_phentime.sql.
-- No row: the index is missing. valid = f: an interrupted build. Either way,
-- run obs_ds_phentime.sql.
SELECT c.relname AS index, i.indisvalid AS valid,
       pg_size_pretty(pg_relation_size(c.oid)) AS size, s.idx_scan AS scans,
       pg_get_indexdef(c.oid) AS definition
  FROM pg_index i
  JOIN pg_class c ON c.oid = i.indexrelid
  LEFT JOIN pg_stat_user_indexes s ON s.indexrelid = i.indexrelid
 WHERE i.indexrelid = to_regclass('public."OBS_DS_PHENTIME"');

-- Expect "Index Only Scan using "OBS_DS_PHENTIME"" (planned, not run).
EXPLAIN SELECT "PHENOMENON_TIME_START" FROM public."OBSERVATIONS"
 WHERE "DATASTREAM_ID" = 1
 ORDER BY "PHENOMENON_TIME_START" DESC
 LIMIT 1;
