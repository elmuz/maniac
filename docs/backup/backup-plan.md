# Backup Review and Remediation Plan

**Status:** proposed  
**Date:** 2026-10-24  
**Scope:** `catafalco.yml`, `host_vars/catafalco/`, the container roles, the backup/restore scripts, and the live host as observed through the MCP tools.

---

## 1. Goals and non-goals

### Goals

- Protect every piece of **irreplaceable service state** (configs, databases, app metadata).
- Protect **user data** that is not already covered by the Julia/Duplicati paths (notably Immich uploads).
- Make the offsite copy **versioned, encrypted, and independently restorable**.
- Make every backup job **observable**: it must report success, failure, and staleness.
- Make restore a **tested procedure**, not a latent Ansible branch.
- Keep the simple parts of the current design where they are good: per-service granularity, Ansible-managed configs, and the existing Julia/Media replication.

### Non-goals

- Replacing SnapRAID. SnapRAID protects against disk failure, not deletion, corruption, or ransomware.
- Backing up regenerable data: container images, Redis, ML model caches, transcodes, logs.
- Turning the media library into an encrypted restic repository. Bulk media remains an rclone copy to Julia unless a second offsite copy is explicitly required.

---

## 2. Current state

### 2.1 What the automation actually does

The repository does **not** currently implement “once a week stop everything and rsync `/srv`”. It implements:

1. Each service role installs a root cron job via `tasks/backup_service.yml`.
2. At 04:00–05:00, `/usr/local/bin/generic_backup.sh`:
   - stops the listed container(s),
   - `rsync -avh --delete /srv/<service>/ /mnt/storage/docker-volumes-backup/<service>/`,
   - starts the container(s).
3. Immich and Dawarich additionally run `pg_dumpall` at 04:50 into `/srv/<service>/backup/latest.sql.gz`.
4. At 08:50, `rclone sync /mnt/storage/docker-volumes-backup julia:catafalco/containers`.
5. Weekly, `rclone sync /mnt/storage/Media julia:media-catafalco` (excluding `/Transcodes/**` and `/Books/**`).
6. Julia → Catafalco mirrors Documents and Pictures; Duplicati appears to be the B2 offsite path for Documents/Pictures.
7. SnapRAID parity sync runs Sunday 03:00.

The result is a **daily per-service mirror** to a local staging directory, followed by a **daily mirror** to Julia. It is not a versioned backup.

### 2.2 Live observations

- 40 containers are running; `containers` in `host_vars/catafalco/vars.yml` lists 28 services.
- `/mnt/storage` is a mergerFS pool (cache + data branches); `/mnt/cache1` is a single NVMe at 68% usage.
- `/mnt/storage/docker-volumes-backup` is on the same mergerFS pool as the source data.
- `swag` and `grafana` are running but have no backup jobs.
- ~~`invidious-db` has no data volume~~; fixed by B-01 (`/srv/invidious/db/data`).
- Immich uploads are configured outside `/srv` at `/mnt/storage/docker-volumes-cache/immich/upload/`.
- `/srv/immich/backup/latest.sql.gz` exists (about 275 MB) and is the only dump kept.
- `/srv/grafana/data/grafana.db` exists (about 3.7 MB) and is not backed up.

---

## 3. Service inventory

### 3.1 Stateful and currently covered

| Service | State | Current backup |
|---|---|---|
| `actual_budget` | `/srv/actual_budget` SQLite | daily 05:00 |
| `arm` | `/srv/arm` config + DB | daily 05:00 |
| `calibreweb` | `/srv/calibreweb` config + Calibre DB | daily 05:00 |
| `deluge` | `/srv/deluge/{config,data}` | weekly Wed 05:00 |
| `duplicati` | `/srv/duplicati/config` (backup jobs + credentials) | daily 05:00 |
| `handbrake` | `/srv/handbrake` config | monthly, 4th at 05:00 |
| `healthchecks` | `/srv/healthchecks/config` SQLite | daily 04:00 |
| `homer` | `/srv/homer` YAML | daily 05:00 |
| `invidious` | `/srv/invidious/db/data` Postgres | daily 05:00 (B-01) |
| `jellyfin` | `/srv/jellyfin` config/metadata | daily 05:00 |
| `lazylibrarian` | `/srv/lazylibrarian` config | daily 05:00 |
| `lidarr` | `/srv/lidarr` config | daily 05:00 |
| `navidrome` | `/srv/navidrome/{data,backup}` | daily 05:00 + internal weekly backup |
| `prowlarr` | `/srv/prowlarr` config | daily 05:00 |
| `radarr` | `/srv/radarr` config | daily 05:00 |
| `slskd` | `/srv/slskd` config/DB | daily 05:00 |
| `sonarr` | `/srv/sonarr` config | daily 05:00 |
| `uptime-kuma` | `/srv/uptime-kuma/data` SQLite | daily 05:00 |

### 3.2 Stateful and partially covered

| Service | Problem |
|---|---|
| `dawarich` | DB dump at 04:50 + stop-all at 05:00. The dump overwrites `latest.sql.gz` non-atomically; a failed dump destroys the previous good copy. No dump retention. |
| `speedtest_tracker` | MariaDB data is copied physically while the DB is stopped. No logical `mysqldump`; restore requires a compatible MariaDB version. |
| `immich` | Postgres is dumped and copied, but the photo upload directory is outside `/srv` and is not backed up. Only `immich-postgres` is stopped; `immich-server` keeps running. |
| `influxdb` | Store is copied while stopped, but a full copy every day of a large time-series store is expensive and has no logical export. |

### 3.3 Stateful and not covered

| Service | State | Why it matters |
|---|---|---|
| `grafana` | `/srv/grafana/data/grafana.db`, plugins, exports | Users, API keys, annotations, alert rules, dashboard versions. Provisioned dashboards are reproducible; user-created content is not. |
| `swag` | `/srv/swag/{nginx,letsencrypt,dns-conf,fail2ban}` | TLS certificates/account keys, nginx base config, fail2ban DB, DNS credentials. Mostly reproducible, but reissuing certs and rebuilding nginx state is avoidable pain. |
| ~~`invidious`~~ | ~~Postgres database, currently in the container writable layer~~ | Resolved by B-01: `/srv/invidious/db/data` is now the Postgres volume and a daily `generic_backup.sh` job exists. |
| `stirling-pdf` | `/srv/stirling-pdf/{extraConfigs,trainingData}` | Settings/fonts; low value but still stateful. No backup job. |

### 3.4 Stateless / regenerable

`socket-proxy`, `watchtower`, `wud`, `nebula-sync`, `flaresolverr`, `whoami`, `influxdb-explorer`, `invidious-companion`, `immich-redis`, `dawarich-redis`, `immich-machine-learning` (model cache). `rclone` config and host services (`mosquitto`, `nut`, `telegraf`) are Ansible-managed and/or regenerable from the vault.

---

## 4. Findings

### P0 — data loss or operational hazards

- **B-P0-1: Invidious DB is not persisted.** — **RESOLVED (B-01).**
  ~~`roles/invidious/tasks/main.yml:35-37` mounts only `db/sql` and the init script. There is no volume for `/var/lib/postgresql/data`. Watchtower or any playbook run with `pull: true` can recreate the container and silently lose the database.~~
  The role now creates `db/data`, mounts it at `/var/lib/postgresql/data`, copies the schema into `db/sql/` (previously it nested into `db/sql/sql`, so the init script could not find it), and installs a daily backup job that stops `invidious-db`.

- **B-P0-2: Immich uploads are outside the backup and on the wrong disk.**
  `roles/immich/templates/.env.j2:4` sets `UPLOAD_LOCATION="{{ mergerfs_root }}/docker-volumes-cache/immich/upload/"`. The generic backup only copies `/srv/immich`, so photos are never copied to Julia. With `category.create=lfs` and the uncache job excluding `{{ cache_root }}/docker-volumes-cache`, new uploads stay on the single cache NVMe, which has no SnapRAID parity.

- **B-P0-3: `generic_backup.sh` can leave containers stopped forever.**
  `files/generic_backup.sh` uses `set -eo pipefail` and has no `trap`. If `docker stop`, `rsync`, or any command fails, the script exits before the restart block. A failed backup at 05:00 can leave a service down until someone notices. There is no lock, so a long run can overlap the 08:50 rclone job.

- **B-P0-4: The restore path is untested and likely destructive.**
  `roles/docker/tasks/main.yml:131-141` uses `src: ".../{{ item }}"`, `dest: "{{ docker_dir }}/"`, and `delete: true` without a trailing slash on `src`. This can copy the service directory into `/srv` and delete unrelated top-level directories. The filter also uses the `containers` list, which contains `uptime_kuma`/`stirling_pdf` while the backup directories are `uptime-kuma`/`stirling-pdf`; `swag` is not in `containers` at all. Restore would skip or delete exactly what is needed.

- **B-P0-5: Database dumps are not atomic and have no retention.**
  Both Immich and Dawarich use `docker exec ... pg_dumpall ... | gzip > .../latest.sql.gz`. The shell truncates `latest.sql.gz` before the dump runs. If the DB is down, the disk is full, or the dump fails, the only good dump is destroyed. There is no `gzip -t`, no temporary file, no dated copy, and no healthcheck.

### P1 — backup architecture weaknesses

- **B-P1-1: The external copy is a mirror, not a backup.**
  `rclone sync` with `--delete` propagates deletions and corruption from Catafalco to Julia. There is no version history, no retention, and no encryption. If a config is deleted or a database is corrupted at the source, the good copy is overwritten. Julia snapshots may mitigate this, but that is not part of this repository’s design and is not verified.

- **B-P1-2: Staging is on the same failure domain as the source.**
  `/mnt/storage/docker-volumes-backup` is on the same mergerFS pool as `/mnt/storage`. SnapRAID protects against disk failure, not deletion, ransomware, or filesystem corruption. A pool-level event can destroy both the source and the staging copy. The only independent copy is Julia, and it is a mirror.

- **B-P1-3: No backup monitoring for per-service jobs.**
  The rclone jobs ping Healthchecks on success, but the per-service `generic_backup.sh` jobs only append to `/var/log/backups/*.log`. A silently failing backup is only discovered when it is needed. There is no freshness check, no failure notification, and no log rotation.

- **B-P1-4: Grafana, SWAG, and Stirling PDF have no jobs.**
  Grafana’s `grafana.db` is real state. SWAG’s certificates and fail2ban state are expensive to rebuild. Stirling PDF is low value but cheap to include.

- **B-P1-5: The backup control plane is not independently protected.**
  Duplicati’s job definitions and encryption settings live in `/srv/duplicati/config`, which is only mirrored to Julia. The B2 credentials and `SETTINGS_ENCRYPTION_KEY` are in the Ansible vault, but the vault password (`~/.vault_pass`, `.vaults`) is local. There is no restic password yet. The vault file is in Git, but the vault password needs separate escrow.

- **B-P1-6: `Media/Books` is excluded from the only Media copy.**
  `roles/rclone/tasks/main.yml:49` excludes `/Books/**`. Duplicati only mounts Documents and Pictures. `Media/Books` exists only on the SnapRAID pool. If that is intentional, document it; if not, fix it.

### P2 — operational improvements

- **B-P2-1: All file-based backups fire at 05:00.** Around 20 jobs stop containers and start rsync at the same minute. This causes an I/O spike, unnecessary concurrent downtime, and no ordering guarantee before the 08:50 rclone.
- **B-P2-2: Physical DB copies are version-fragile.** Copying `/srv/immich/postgres` and `/srv/dawarich/db_data` is only restorable into a compatible Postgres major version. Logical dumps should be the primary restore path.
- **B-P2-3: No restore drill.** There is no documented procedure and no test that restores a service into a scratch directory or host.
- **B-P2-4: No explicit excludes.** Backups include logs, model caches, and other regenerable files, increasing staging size, rclone time, and B2 cost.
- **B-P2-5: `rsync` does not preserve ACLs/xattrs by default.** `-a` preserves ownership/permissions/times, but not ACLs/xattrs. Consider `-A -X` or restic, and use `--numeric-ids` for container data.

---

## 5. Target architecture

```
Postgres/MariaDB ──(pg_dump/mysqldump, online)──> /srv/<service>/backup/YYYY-MM-DD.sql.gz
SQLite/file services ──(short stop or sqlite3 .backup)──> /srv/<service>
/srv (configs + dumps) + Immich uploads + SWAG certs
        │
        └──(restic, encrypted, deduplicated)──> B2/S3, retention + restic check

/mnt/storage/Media ──(rclone, weekly)──> julia:media-catafalco (bulk, versioned by NAS snapshots)
Documents/Pictures <──(rclone)──> julia + Duplicati B2
Healthchecks <── success/failure pings from every job; stale-backup alert
```

The existing `roles/restic` already provides the repository/config/retention pattern used on `nilde`. Its `stop_services()` currently stops **all** containers, which is too blunt for catafalco; adapt it before enabling it here.

---

## 6. Execution plan

### Phase 0 — stop the bleeding (target: this week)

- [x] **B-01 — Persist and back up the Invidious DB.**
  > **Implemented 2026-10-25 (deploy + live verification pending).** The service is not in use, so the pre-migration dump was
  > intentionally skipped (there is no data worth preserving). The role now creates
  > `/srv/invidious/db/data`, mounts it at `/var/lib/postgresql/data`, copies the SQL
  > schema into `/srv/invidious/db/sql/` (fixing the pre-existing `sql/sql` nesting that
  > prevented initialisation), and adds the standard daily `backup_service.yml` job that
  > stops `invidious-db`. Deploy with `ansible-playbook catafalco.yml -K --tags invidious`,
  > then verify the cluster appears under `/srv/invidious/db/data` and survives a
  > `docker rm -f invidious-db` + playbook run.
  - Dump the current DB before changing the container (not needed here — service unused):
    ```sh
    docker exec -t invidious-db pg_dump -U funkysloth -d invidious -Fc \
      > /srv/invidious/db/pre-volume-migration.dump
    ```
  - Create `/srv/invidious/db/data` and add:
    ```yaml
    volumes:
      - "{{ docker_dir }}/{{ invidious_container_name }}/db/sql:/config/sql"
      - "{{ docker_dir }}/{{ invidious_container_name }}/db/init-invidious-db.sh:/docker-entrypoint-initdb.d/init-invidious-db.sh"
      - "{{ docker_dir }}/{{ invidious_container_name }}/db/data:/var/lib/postgresql/data"
    ```
  - Recreate the container, restore the dump if the init scripts did not preserve it, and verify users/subscriptions.
  - Add a normal `backup_service.yml` job for `invidious`, stopping `invidious-db`.
  - **Done when:** `/srv/invidious/db/data` contains the Postgres cluster, a container recreate keeps the data, and the backup job exists.

- [ ] **B-02 — Bring Immich uploads into the backup.**
  - Choose one:
    - **Preferred:** move the uploads to the data pool (`/mnt/storage_slow/immich/upload/`) so they are on SnapRAID-protected disks, then include that path in restic; or
    - keep them on the cache SSD and explicitly include `/mnt/storage/docker-volumes-cache/immich/upload` in the offsite backup, accepting that the cache drive has no parity.
  - Migration (preferred path):
    ```sh
    # Stop Immich application containers first.
    rsync -aHAX --info=progress2 \
      /mnt/storage/docker-volumes-cache/immich/upload/ \
      /mnt/storage_slow/immich/upload/
    # Update roles/immich/templates/.env.j2, redeploy Immich, verify, then
    # remove the old directory after a retention period.
    ```
  - Extend the Immich backup to stop `immich-server` as well if uploads are copied with rsync, or use a filesystem snapshot.
  - **Done when:** a restore of Immich requires only the restic snapshot + DB dump, and the upload path is documented.

- [ ] **B-03 — Add missing backup jobs.**
  - `grafana`: daily, stop `grafana`, rsync `/srv/grafana`.
  - `swag`: daily or weekly; either stop briefly and rsync `/srv/swag`, or use restic while running (certs/config are mostly stable).
  - `stirling_pdf`: weekly, low priority.
  - **Done when:** each service has a job, a log, and a Healthchecks ping.

- [ ] **B-04 — Harden `files/generic_backup.sh`.**
  - Add a restart trap:
    ```sh
    STARTED=()
    restart_containers() {
      for c in "${STARTED[@]}"; do
        docker start "$c" || log_error "Failed to start $c"
      done
    }
    trap restart_containers EXIT
    ```
  - Add `flock` to prevent overlapping runs.
  - Use `docker stop --time "${STOP_TIMEOUT:-60}"` for DB-backed services.
  - Assert the destination is on the expected mergerFS pool, not a plain directory on the root filesystem:
    ```sh
    findmnt -rn -T "$DESTINATION_PATH" -o FSTYPE | grep -qx fuse.mergerfs || {
      log_error "$DESTINATION_PATH is not on the mergerFS pool"; exit 1;
    }
    ```
  - Check free space; continue restarting all containers even if one step fails; ping Healthchecks on success/failure.
  - Add log rotation for `/var/log/backups/*.log`.
  - **Done when:** killing the script after `docker stop` still restarts the containers, and a failed rsync produces a non-zero exit plus an alert.

- [ ] **B-05 — Make DB dumps atomic and retained.**
  Replace the inline cron jobs with a small script:
  ```sh
  set -euo pipefail
  dest="/srv/immich/backup"
  tmp="$(mktemp "${dest}/.immich-XXXXXX.sql.gz")"
  docker exec -t immich-postgres pg_dumpall --clean --if-exists --username=postgres | gzip > "$tmp"
  gzip -t "$tmp"
  mv "$tmp" "${dest}/immich-$(date +%F).sql.gz"
  find "$dest" -name 'immich-*.sql.gz' -mtime +14 -delete
  curl -fsS .../ping/<uuid>
  ```
  Apply the same pattern to Dawarich. Keep `latest.sql.gz` only as a convenience symlink, never as the only copy.
  - **Done when:** a failed dump leaves the previous dated dump intact, and the job alerts on failure.

- [ ] **B-06 — Fix or disable the restore path.**
  - Do not rely on `docker_restore_volumes_backup` as-is.
  - Use trailing slashes and restore into the service directory, not `/srv` root.
  - Avoid `delete: true` on `/srv`.
  - Restore only after stopping the target containers.
  - Use `--numeric-ids`.
  - Filter on actual backup directory names, not the `containers` list.
  - Include `swag` and any other infrastructure service explicitly.
  - **Done when:** a test restore into a scratch directory reproduces the service without deleting unrelated data.

- [ ] **B-07 — Escrow the recovery secrets.**
  Store the Ansible vault password, the restic password, and the Duplicati encryption passphrase in a password manager and/or a sealed physical copy. A backup that cannot be decrypted is not a backup.
  - **Done when:** a second person can obtain the secrets without access to `catafalco`.

- [ ] **B-08 — Add backup freshness monitoring.**
  - Add a Healthchecks ping or a single “backup freshness” check that verifies each expected service directory has a file newer than its RPO.
  - Alert if a job does not run, fails, or produces no new data.
  - **Done when:** deleting a staging directory or breaking one backup job triggers an alert within the expected window.

### Phase 1 — build the real offsite backup (target: 1–4 weeks)

- [ ] **B-09 — Enable restic on catafalco.**
  - Reuse `roles/restic`, but adapt `stop_services()` so it does not stop every container.
  - Repository: B2/S3, separate bucket/prefix from `nilde`.
  - Paths: `/srv` (configs and the per-service `backup/` dumps) and the Immich upload path.
  - Exclude: logs, caches, `models`, `Transcodes`, and physical DB directories where a logical dump is the primary restore path.
  - Retention: e.g. `--keep-daily 7 --keep-weekly 4 --keep-monthly 12 --keep-yearly 2 --prune`.
  - Run `restic check` weekly and `restic check --read-data-subset=5%` monthly.
  - **Done when:** a snapshot can be listed, checked, and restored from a fresh host.

- [ ] **B-10 — Move to online logical DB dumps.**
  - Postgres: `pg_dumpall`/`pg_dump` without stopping the DB.
  - MariaDB: `mysqldump --single-transaction`.
  - SQLite: `sqlite3 .backup` or a short stop.
  - Stop containers only for file-based state that cannot be quiesced otherwise.
  - **Done when:** Postgres/MariaDB services no longer need a full stop for a consistent backup.

- [ ] **B-11 — Stagger and sequence the schedules.**
  Use systemd timers or a single orchestrator with locks:
  - 04:00 DB dumps,
  - 04:30 file-based services,
  - 05:30 restic,
  - 08:00 rclone/Media,
  - Sunday 03:00 SnapRAID (already exists).
  Ensure the offsite copy starts only after all dumps/backups finish.
  - **Done when:** no two heavy jobs overlap and rclone never sees a partial backup.

- [ ] **B-12 — Run a restore drill.**
  Restore at least Grafana, Immich (DB + one photo), Sonarr, and SWAG into a scratch environment. Record the steps and timings in this document.
  - **Done when:** the drill succeeds and the runbook is updated with any deviations.

- [ ] **B-13 — Decide the `Media/Books` policy.**
  Either include `/Books` in the Media rclone job or document why it is excluded and what protects it.
  - **Done when:** the exclusion is intentional and documented, or removed.

### Phase 2 — keep it true (ongoing)

- [ ] **B-14 — Add capacity alerts.**
  Monitor `/`, `/mnt/storage`, `/mnt/cache1`, and the restic repository size. Alert before the cache or the repository grows into a failure.
- [ ] **B-15 — Review retention and cost quarterly.**
  Adjust `--keep-*`, B2 bucket size, and the Media policy.
- [ ] **B-16 — Re-run the restore drill quarterly.**
  Rotate the drill target and verify the runbook still matches the repo.
- [ ] **B-17 — Document RPO/RTO and exclusions.**
  Add a short “what is not backed up” section to this document and to the operator notes.

---

## 7. Migration notes and warnings

### Invidious volume migration

**Prepared in B-01.** Invidious is not in use, so no dump/restore is required: the container
can be recreated directly against the new `/srv/invidious/db/data` volume. The image initialises
a fresh cluster and the init script loads the schema from `/config/sql`.

If this ever has to be repeated on a used instance, dump first with
`docker exec -t invidious-db pg_dump -U funkysloth -d invidious -Fc > /srv/invidious/db/pre-volume-migration.dump`,
then restore with `pg_restore` after the new volume exists.

### Immich upload migration

Stop `immich-server` and `immich-machine-learning` before moving the upload directory. Do not run the move while uploads are in progress. After the move, update `.env`, redeploy, upload one test photo, and verify the file appears under the new path. Keep the old directory for at least one retention period.

### Database dump migration

Replace the old cron entries atomically: deploy the new script and remove the old cron job in the same Ansible run. Do not leave both active, or two dumps will race for the same destination.

### Restore path

`roles/docker/tasks/main.yml` currently has `docker_restore_volumes_backup: false` by default. Keep it false until the corrected restore path has been tested. A restore should be a separate, explicit operation, not a side effect of a normal playbook run.

---

## 8. Restore runbook (high level)

1. Rebuild the host with the same Ansible repository and vault.
2. Restore `/srv` from restic (or the per-service staging as a fallback).
3. Restore the Immich upload directory to the configured `UPLOAD_LOCATION`.
4. Restore DB dumps and start databases before application containers.
5. Start containers in dependency order (`db` → `redis` → app → sidecars).
6. Verify health endpoints, logs, and one real user action per critical service.
7. Record the restore date and any manual steps.

Use the MCP remote-server tools for live checks where possible; do not rely on ad-hoc SSH commands.

---

## 9. Open decisions

- **RPO/RTO:** Is 24 h acceptable for service state, or do critical DBs need more frequent dumps?
- **Immich uploads:** Keep on the cache SSD for speed, or move to the parity-protected data pool?
- **Media offsite:** Is the Julia copy enough, or does Media need a second offsite copy (B2 cost)?
- **Duplicati vs restic:** Keep Duplicati for Documents/Pictures and add restic for service state, or consolidate on restic?
- **Downtime tolerance:** Are short per-service stops acceptable at 05:00, or should the target be fully online DB dumps with no stops?
- **Retention/budget:** How many daily/weekly/monthly versions do you want to pay for?

---

## 10. Appendix A — target schedule

| Time | Job |
|---|---|
| 03:00 Sunday | SnapRAID sync (existing) |
| 04:00 | Online DB dumps (Postgres/MariaDB) |
| 04:30 | File-based service backups (staggered) |
| 05:30 | restic to B2/S3 |
| 08:00 | rclone Media → Julia |
| Weekly | `restic check` |
| Monthly | `restic forget --prune` + `restic check --read-data-subset` |
| Quarterly | Restore drill |

## 11. Appendix B — verification checklist

- [ ] `restic snapshots` lists the latest backup and the expected paths.
- [ ] `restic check` passes.
- [ ] Every expected service directory has a file newer than its RPO.
- [ ] Healthchecks shows a recent success for every backup job.
- [ ] A test restore of one config-only service and one DB-backed service succeeds.
- [ ] The Invidious DB survives a container recreate.
- [ ] The Immich upload directory is present in the restic snapshot.
- [ ] The restore role no longer uses `delete: true` against `/srv`.
- [ ] The vault password, restic password, and Duplicati passphrase are escrowed.
