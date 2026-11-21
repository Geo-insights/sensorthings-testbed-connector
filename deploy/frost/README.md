# FROST database: what FROST-Server does not create

The FROST stack (`frost.geo-insights.nl`: services `frost-server`, `frost-db`, `mcp`, `caddy`)
runs from `/opt/frost` on 49.12.222.122. That directory is not a git checkout: its
`docker-compose.yml`, `Caddyfile`, `.env` and `mcp.env` are kept by hand on the server, and
its one checkout, `/opt/frost/sensorthings-testbed-connector`, is this repository (the MCP
server's build context). FROST-Server creates its own tables and indexes (Liquibase) when it
starts on an empty database; whatever else that database needs is kept here, so it can be put
back after a rebuild.

| File | What it does |
|------|--------------|
| [`obs_ds_phentime.sql`](obs_ds_phentime.sql) | Builds the index `OBS_DS_PHENTIME`. Writes; idempotent. |
| [`check_obs_ds_phentime.sql`](check_obs_ds_phentime.sql) | Is the index there, valid and used? Read-only. |
| [`progress.sql`](progress.sql) | A running build and the session it waits for. Read-only. |

## OBS_DS_PHENTIME

```sql
CREATE INDEX "OBS_DS_PHENTIME" ON public."OBSERVATIONS" USING btree ("DATASTREAM_ID", "PHENOMENON_TIME_START" DESC)
```

A Datastream's newest Observation in one index probe. FROST-Server 2.8 indexes
`"OBSERVATIONS"` on its primary key, `"DATASTREAM_ID"`, `"FEATURE_ID"` and
`("DATASTREAM_ID", "ID")` (`"OBS-DS_ID-ID"`) only; none of its Liquibase changesets creates
this one. Without it, "the newest Observation of Datastream X" reads and sorts all of X's
rows, and these readers run into the database's 60 s `statement_timeout`:

- the connector's restart seeding (`app/main.py`, `_SEED_NEWEST_OBSERVATION`): each page of
  500 Datastreams expands every Datastream's newest Observation;
- the platform's latest values (`geo-insights-platform`, `apps/api/app/observations.py`,
  `latest_many`) and monitoring's (`monitoring_module/services/frost_reads.py`):
  `ORDER BY "PHENOMENON_TIME_START" DESC LIMIT 1` per Datastream.

Production, 2026-11-20: the index is valid, 951 MB, with 1.19 M scans, on an
`"OBSERVATIONS"` of 22.3 M rows (8 GB table, 10 GB with indexes). FROST created the schema on
2026-11-11; this index was added by hand afterwards. The definition above is copied from
production's `pg_indexes`.

### When to run it

- **After FROST's database is created anew**: a new or emptied `frost_pgdata` volume
  (`docker compose down -v`, a move to another server). Run it once `frost-server` has
  started and created its tables (the file refuses to run before that), preferably before
  the connector posts again: on an empty table it takes a second.
- **After a restore.** A full `pg_dump`/`pg_restore` brings the index along; a data-only
  restore into a schema FROST created does not. Run the check.
- **After upgrading FROST-Server or PostgreSQL.** Run the check.
- **When latest values get slow**: the connector logs `Seeded ... incomplete` or `newest
  Observations not expanded` after a restart, or the platform answers latest-value reads
  with a timeout. Run the check.

The check takes a second and changes nothing. If it shows no row, or `valid = f`, run the
build. Running the build when the index is fine is a no-op.

Not `docker-entrypoint-initdb.d`: PostgreSQL's init scripts run once, on an empty data
directory, before `frost-server` has created `"OBSERVATIONS"`.

### How to run it

From a checkout of this repository, with the Hetzner key. `\${POSTGRES_USER}` and
`\${POSTGRES_DB}` are expanded inside the `frost-db` container, which has them; nothing
prints them.

```bash
# Check (read-only)
ssh -i ~/.ssh/id_hetzner root@49.12.222.122 \
  'cd /opt/frost && docker compose exec -T frost-db sh -c "psql -X -U \${POSTGRES_USER} -d \${POSTGRES_DB}"' \
  < deploy/frost/check_obs_ds_phentime.sql

# Build (writes)
ssh -i ~/.ssh/id_hetzner root@49.12.222.122 \
  'cd /opt/frost && docker compose exec -T frost-db sh -c "psql -X -v ON_ERROR_STOP=1 -U \${POSTGRES_USER} -d \${POSTGRES_DB}"' \
  < deploy/frost/obs_ds_phentime.sql
```

Or on the server, from its checkout of this repository (pulling it rebuilds nothing):

```bash
cd /opt/frost
git -C sensorthings-testbed-connector pull --ff-only
docker compose exec -T frost-db sh -c 'psql -X -v ON_ERROR_STOP=1 -U "$POSTGRES_USER" -d "$POSTGRES_DB"' \
  < sensorthings-testbed-connector/deploy/frost/obs_ds_phentime.sql
```

Never add `-1`/`--single-transaction`: `CREATE INDEX CONCURRENTLY` refuses to run in a
transaction block.

What the build prints: `CREATE INDEX` and a last row `OBS_DS_PHENTIME | t | <size> | <the
definition above>`. When the index was already there, `NOTICE: relation "OBS_DS_PHENTIME"
already exists, skipping` comes first; after an interrupted build, `DROP INDEX` comes first.
It exits 0 only when the index is valid at the end, otherwise 3 with an `ERROR:` line:

| `ERROR:` | Meaning, what to do |
|----------|---------------------|
| `public."OBSERVATIONS" does not exist` | FROST has not created its tables yet. Start `frost-server`, wait until it answers, run it again. |
| `an index "OBS_DS_PHENTIME" exists with another definition` | Someone made a different index under this name. Find out why; drop it by hand (`DROP INDEX CONCURRENTLY public."OBS_DS_PHENTIME";`) only if it is not needed, then run it again. |
| `canceling statement due to statement timeout`, or the connection dropped | The build stopped halfway and left an invalid index (`valid = f`). Run it again: it drops the leftover and builds anew. |

### On a full table

`CONCURRENTLY` keeps FROST reading and writing during the build. The build reads the whole
table twice and sorts 22 M entries (`maintenance_work_mem` is 256 MB), so expect minutes, and
about 1 GB of disk for the index plus temporary sort files (the server had 50 GB free on
2026-11-20). Before it finishes, it waits for every transaction older than itself; FROST's
queries end within the 60 s timeout, a session left idle in a transaction does not.

The file sets `statement_timeout = 0` for its own session: under the database default (60 s)
the build is cancelled and leaves an invalid index, and a plain
`CREATE INDEX CONCURRENTLY IF NOT EXISTS` run afterwards skips that invalid index. On a full
table, run it on the server inside `tmux`, or run it again if the connection drops.

Watch a build from a second session (read-only):

```bash
ssh -i ~/.ssh/id_hetzner root@49.12.222.122 \
  'cd /opt/frost && docker compose exec -T frost-db sh -c "psql -X -U \${POSTGRES_USER} -d \${POSTGRES_DB}"' \
  < deploy/frost/progress.sql
```

`phase` says where it is (`building index: ...`, `index validation: ...`, `waiting for
...`); `waits_for` is the session it waits on, with what that session is doing.

### Removing it

`DROP INDEX CONCURRENTLY public."OBS_DS_PHENTIME";` frees about 1 GB. The readers above then
sort whole Datastreams again and time out; build it again with the file.

### Tested

FROST-Server 2.8 on `postgis/postgis:16-3.4-alpine` with 600 k Observations and the
database's `statement_timeout` at 60 s, as on production: the build from scratch, a second
run (no-op), the invalid index a timed-out build leaves (dropped and built again while an
older transaction held the table for 75 s), an index of this name with another definition
and a database without FROST's tables (both stop with exit 3 and leave things as they were),
and the file with CRLF line endings.
