# TODO / open questions

Working list of things to verify or fix. Each entry records what was measured, on
which host, and what is still unknown — so it can be picked up without redoing the
investigation.

---

## InfluxDB store growth vs. 90-day retention

**Raised:** 2026-10-25
**Host:** catafalco
**Status:** answered (projection), one open item (file count)
**Related:** `docs/storage-review.md` P0-4 (the daily backup copies the whole store)

### Question

`/srv/influxdb/db` was already ~450 MB after a couple of days of InfluxDB running.
Is the 90-day projection simply `450 MB × 45 ≈ 20 GB`?

### Answer: no — steady state is ~8–9 GB, not ~20 GB

`system.parquet_files` on 2026-10-25 (store covers 2026-10-21 22:30 UTC →
2026-10-25 14:01 UTC, **3.65 days**):

| Component | Size |
|---|---|
| Parquet (durable data) | 323 MB, 8.46 M rows |
| WAL + catalog + snapshots + db-indices | ~127 MB (recycled, not linear) |
| Total `/srv/influxdb/db` | ~450 MB |

Measured rates:

- **88.6 MB/day** of parquet
- **2.32 M rows/day**
- **~3 480 parquet files/day**

Projection: `88.6 MB × 90 ≈ 8.0 GB` of parquet plus ~0.1–0.2 GB of roughly constant
overhead → **~8–9 GB steady state**. It does not keep growing past that, because
`influxdb_retention_period: 90d` drops expired data (and its files). The `450 × 45`
figure is wrong twice: it assumes 225 MB/day instead of 88.6 MB/day of durable data,
and it counts the WAL as if it accumulated instead of being compacted away.

### Where the space goes

The six `docker_container_*` tables are **233 MB / 7.5 M rows**, i.e. ~72 % of the
space and ~89 % of the rows. Levers, if the store ever needs to shrink:

- `[[inputs.docker]] interval` 10 s → 60 s: cuts ~62 % of all rows (~3 GB at 90 days).
- shorter `influxdb_retention_period`.

### Open item: parquet file count vs. the query file-scan limit

12 699 parquet files in 3.65 days extrapolates to **~313 k files at 90 days**
(~165 files/table/day, ~one flush every 9 min). Day 22/23/24 all hold ~3 480 files,
so within this window nothing merged the older files. The role sets
`INFLUXDB3_QUERY_FILE_LIMIT: "2000"` (`roles/influxdb/tasks/main.yml`), which is
roughly **~12 days of a single measurement** — long-range panels (30 days) are at
risk of HTTP 500 "file scan limit", while 30-minute dashboard ranges are fine.

To check later:

1. Re-query the per-day file counts after a week or two: if older days shrink, the
   compactor is merging and the limit is not a practical problem.
2. If they do not shrink, either raise `INFLUXDB3_QUERY_FILE_LIMIT`, reduce the
   docker input frequency, or accept that long-range queries need tighter time
   ranges.

Note this interacts with P0-4 in `docs/storage-review.md`: an ~8–9 GB store is also
what gets `rsync --delete`'d into `docker-volumes-backup` and mirrored to julia every
day if the backup source is not narrowed to the configuration only.

**Resolved (P0-4):** the InfluxDB service backup now passes an rsync exclude for
`/db/` (`influxdb_backup_exclude_paths`), and `generic_backup.sh` uses
`--delete-excluded` so the store copy left by previous runs is removed from
`docker-volumes-backup` on the next run. Only `admin-token.json`, `explorer/` and
`plugins/` are backed up, and the container is no longer stopped for the job.

**Retention changed:** catafalco now sets `influxdb_retention_period: "30d"` in
`host_vars/catafalco/vars.yml`. The 90-day projection above therefore no longer
applies to this host; expect roughly `88.6 MB/day × 30 ≈ 2.7 GB` of parquet at
steady state.
