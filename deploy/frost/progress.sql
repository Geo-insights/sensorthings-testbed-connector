SET default_transaction_read_only = on; SET statement_timeout = '50s';
-- Read-only: a running CREATE INDEX CONCURRENTLY, and the session it waits for.
-- No row: no build is running.
SELECT p.pid, p.phase,
       round(100.0 * p.blocks_done / nullif(p.blocks_total, 0), 1) AS blocks_pct,
       p.current_locker_pid AS waits_for, a.state AS waits_for_state,
       now() - a.xact_start AS waits_for_open, left(a.query, 60) AS waits_for_query
  FROM pg_stat_progress_create_index p
  LEFT JOIN pg_stat_activity a ON a.pid = p.current_locker_pid;
