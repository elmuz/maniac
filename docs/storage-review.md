# Storage and data-protection review — catafalco

**Date:** 2026-10-25  
**Scope:** current storage layout, data-protection layers, backup/sync jobs, and the gap between the intended design and what the repository/host actually does.  
**Status:** analysis only. No host or repository configuration was changed.  
**Revision:** priorities adjusted after feedback: julia snapshot coverage was confirmed (media weekly/retention 1, containers smart retention/14 today), the one-way julia→catafalco Documents/Pictures overwrite is accepted, and the service-backup finding was downgraded from “no versioning” to “local-only versioning”.

This review deliberately does **not** re-investigate the `/mnt/storage/Downloads` disappearance. It looks at the system as it is now and at the failure modes that could turn a recoverable event into permanent loss.

---

## 1. Verdict

The array itself is healthy:

- All six disks report SMART `PASSED`.
- SnapRAID reports no errors and a current sync.
- Data usage is low (about 26–27 % per data disk, 28 % on the pool).
- Cache/NVMe wear is low (8 % life used on Cache1, 4 % on NVMe2).
- The important data (Documents/Pictures) has three copies: julia, catafalco, Backblaze B2.

The weak points are not disk health. They are **operational semantics**:

1. **A missing mount is not detected by the jobs that move or mirror data.** `rclone sync`, the uncache script, and SnapRAID can all interpret “a disk/branch is not mounted” as “the files were deleted”.
2. **SnapRAID’s mass-deletion guard is disabled** (`snapraid_deletethreshold: -1`).
3. **The InfluxDB backup copies the entire InfluxDB store every day**, which is a major source of SnapRAID churn and an unnecessary daily disk load.
4. **The uncache script can move active/incomplete downloads and seeding files**, because it only excludes `docker-volumes-cache`.
5. **By design, the btrfs snapshots on catafalco are not a weekly/monthly history.** They are one frozen view used by SnapRAID; `snapraid-btrfs cleanup` deletes older snapraid snapshots after each successful sync. This is only a gap if you later decide catafalco itself needs more history.
6. **The media and docker-volumes copies on julia are mirrors with snapshot history** (media: weekly, retention 1; containers: smart retention, 14 snapshots today). Versioning exists, but a mass deletion still propagates until the next snapshot/retention point.

The highest-value work is therefore **guarding data-moving jobs and making the existing snapshots/mirrors harder to poison**, not buying more disks.

---

## 2. Verified current state

### 2.1 Mounts and capacity

Observed through the MCP mount/disk tools on 2026-10-25:

| Mount | Device / source | FS | Size | Used | Use |
|---|---|---:|---:|---:|---:|
| `/` | `/dev/sdd2` | ext4 | 415 GiB | 136 GiB | 35 % |
| `/mnt/cache1` | `/dev/nvme0n1` | ext4 | 916 GiB | 618 GiB | 67–71 % |
| `/mnt/data1` | `/dev/sdc1[/data]` | btrfs | 16.4 TiB | 4.4 TiB | 27 % |
| `/mnt/data2` | `/dev/sdb1[/data]` | btrfs | 16.4 TiB | 4.3 TiB | 27 % |
| `/mnt/parity1` | `/dev/sda1` | ext4 | 16.3 TiB | 4.5 TiB | 28 % |
| `/mnt/storage` | mergerfs (cache + data) | fuse.mergerfs | 33.6 TiB | 9.3 TiB | 28 % |
| `/mnt/storage_slow` | mergerfs (data only) | fuse.mergerfs | 32.7 TiB | 8.7 TiB | 27 % |
| `/mnt/storage_cache` | mergerfs (cache only) | fuse.mergerfs | 916 GiB | 618 GiB | 67–71 % |

The union is configured in `roles/mounts/tasks/main.yml` as:

- `/mnt/storage` = `/mnt/cache*:/mnt/data*`, `category.create=lfs`, `minfreespace=50G`
- `/mnt/storage_slow` = `/mnt/data*`, `category.create=mfs`, `minfreespace=500G`
- `/mnt/storage_cache` = `/mnt/cache*`, `category.create=mfs`

This matches the intended “fill the cache first, demote to the slow pool” behaviour: `lfs` picks the branch with the least free space, and the cache has far less free space than the data disks.

### 2.2 Disk health

| Label | Model | POH | Life used / health | Notes |
|---|---|---:|---|---|
| Cache1 | CT1000P2SSD8 | 37,493 h (4.3 y) | 8 % used, PASSED | 34 °C, 0 media errors, 199 unsafe shutdowns |
| NVMe2 | KINGSTON SNV3S1000G | 4,275 h | 4 % used, PASSED | 37 °C, spare 98 % |
| Data1 | ST18000NT001 | ~15,126 h | PASSED | 28 °C, standby between polls |
| Data2 | ST18000NT001 | ~15,123 h | PASSED | 28 °C, standby between polls |
| Parity1 | ST18000NT001 | 15,044 h | PASSED | 29 °C, 0 reallocated/pending/CRC |
| System | CT480BX500SSD1 | 8,536 h | PASSED | 29 °C |

The “stale SMART sample” warnings for Data1/Data2 are expected: the disks are in standby and Telegraf skips the poll (`nocheck=standby`). The last valid sample is still `PASSED`.

### 2.3 SnapRAID

From `snapraid status`:

- 145,289 files, 9,535 GB used, 26,102 GB free.
- 2,507 fragmented files / 17,870 excess fragments.
- No error detected, no sync in progress.
- 12 % of the array not scrubbed; oldest block scrubbed 70 days ago.
- 47,513 files with a zero sub-second timestamp.

Scrub coverage is acceptable for a weekly 12 % plan; the zero-timestamp count is cosmetic but creates noisy diffs.

### 2.4 Monitoring caveat

InfluxDB currently contains only data since **2026-10-21**, although the database is configured with 90-day retention. This is consistent with the store/database having been recreated recently. As a result, this review cannot show a real 90-day growth trend for the cache or the data disks.

Also, `roles/hd_idle/files/hd_idle_parser.py` exists, but the role does not deploy or schedule it. The `disk_state` measurement does not exist in InfluxDB, so there is currently **no historical evidence of how often the mechanical disks actually sleep**. That matters for the power/longevity goal, but it is a monitoring gap rather than a data-risk issue.

---

## 3. Intended design vs. actual configuration

| Topic | Intended / assumed | Actual in repo/host | Consequence |
|---|---|---|---|
| Cache target | Fill cache to ~80 %, then demote | Uncache target is **75 %**, and it runs **Mon–Sat 07:00 only** (`roles/mergerfs/tasks/main.yml:30-36`) | Cache stays around 70–75 % and can hold a large amount of unprotected data |
| Cache exclusions | Deluge seeding stays in cache; incomplete downloads not moved | Only `{{ cache_root }}/docker-volumes-cache` is excluded | Incomplete torrents/slskd files and active seeds can be moved to the slow pool when the script runs |
| Uncache safety | Refuses to run unless both pools are mounted | Only `Path.is_dir()` is checked | An unmounted mountpoint still exists as a directory on `/`, so the script can copy into a hidden root-filesystem directory and `--remove-source-files` |
| SnapRAID deletion guard | Protects against accidental mass deletion | `snapraid_deletethreshold: -1` (`host_vars/catafalco/vars.yml:311`) | A mass deletion is synced into parity without warning |
| SnapRAID snapshots | “Smart snapshots” provide deletion recovery | `snapraid-btrfs cleanup` deletes every snapraid snapshot older than the most recent synced one | Only the current sync interval is recoverable from catafalco snapshots; no weekly/monthly history |
| Media to julia | Weekly backup / accidental-deletion copy | `rclone sync /mnt/storage/Media julia:media-catafalco`; julia keeps a weekly snapshot with retention 1 | It is a mirror, but the previous weekly snapshot gives about one week of deletion recovery. A mass deletion can still propagate and consume the only retained snapshot. |
| docker-volumes to julia | Daily backup | `rclone sync` of a single current catafalco copy; julia keeps smart-retention snapshots (14 today) | Versioning exists on julia; the catafalco copy itself is still a single overwritten copy, and there is no offsite copy. |
| Documents/Pictures from julia | julia is the entry point | `rclone sync julia:... /mnt/storage/...` **and** Samba exports those directories writable | Accepted one-way behaviour: writes on catafalco are intentionally overwritten. Not a defect, but it should be documented so it is not mistaken for data loss. |
| Service backup | Daily copy of `/srv` service configs | `generic_backup.sh` uses `rsync -avh --delete` into one catafalco directory, then rclone mirrors it to julia | Catafalco has one current copy; julia snapshots provide version history. Remaining gap is offsite/restore testing, not total lack of versions. |
| InfluxDB backup | Back up the service configuration | Source is the whole `/srv/influxdb`, which contains the `db` store | The entire metrics store is copied daily, generating huge churn and mass-deletion diffs |

---

## 4. Corrected protection map

| Data | Copies now | Versioning / deletion recovery | Realistic RPO | Main remaining risk |
|---|---|---|---|---|
| Documents | julia + catafalco + B2 | julia internal snapshots, Duplicati versions on B2 | ~1 day | None material; direct writes on catafalco are intentionally discarded by the julia→catafalco sync |
| Pictures | julia + catafalco + B2 | julia internal snapshots, Duplicati versions on B2 | ~1 day | Same as Documents |
| Media | catafalco + julia | julia mirror plus one weekly snapshot (retention 1); catafalco has one SnapRAID sync snapshot | up to ~1 week | `rclone sync` can propagate a mass deletion; the single julia snapshot can be rotated away |
| docker-volumes-backup | catafalco pool + julia | julia smart-retention snapshots (14 today); catafalco current copy unversioned | ~1 day | No offsite copy; restore from julia snapshots untested; catafalco copy can be overwritten |
| InfluxDB store | catafalco `/srv` + copied into docker-volumes-backup + julia | None | ~1 day | Not important data, but it is a major source of churn |
| Downloads | cache1 | None (transient by design) | n/a | Cache failure loses in-flight data; uncache can move incomplete files |

**Important correction:** the catafalco btrfs snapshots are not a retention scheme. The `snapraid-btrfs` cleanup logic deletes all snapraid snapshots older than the most recent synced snapshot. They exist so `scrub`/`check`/`fix` can read a frozen view, not so you can go back several weeks. Long-term deletion recovery comes from julia/B2/Duplicati, not from catafalco snapshots.

Julia is different: the media share has a weekly snapshot with retention 1, and the containers share has smart retention (14 snapshots today). Those snapshots are real history, but they are still local to julia and their retention is the limiting factor.

---

## 5. Risks in priority order

### P0 — fix before the next unattended maintenance window

#### P0-1. Missing mounts can cascade into data deletion

**Evidence**

- `rclone sync` is used for both directions (`roles/rclone/tasks/main.yml:23,48,66`).
- The uncache script checks only `is_dir()` (`roles/mergerfs/templates/mergerfs-uncache.j2`, general checks).
- SnapRAID data paths are plain directories; the runner has no preflight mount check.

**Why it matters**

There are several realistic chains:

1. `/mnt/storage` or one mergerfs branch is not mounted at boot. `rclone sync /mnt/storage/Media julia:media-catafalco` sees a smaller/empty source and mirrors the loss to julia.
2. `/mnt/storage_slow` is not mounted. The uncache script copies files into the hidden directory on `/` and removes the source files, so they disappear from the merged view while consuming root-disk space.
3. `/mnt/data1` is not mounted during a SnapRAID run. `snapraid diff` sees the files as removed; with `deletethreshold: -1`, it syncs those removals into parity.
4. The same missing branch makes `/mnt/storage/docker-volumes-backup` look incomplete, and the next `rclone sync` deletes the julia copy.

This is the class of failure that turns a recoverable event into permanent loss.

**Recommended fix**

Add one shared preflight script (for example `/usr/local/sbin/storage-preflight`) and call it from:

- the uncache cron job,
- every `rclone` cron job,
- the SnapRAID runner service (`ExecStartPre=` or at the top of the runner wrapper),
- optionally `generic_backup.sh`.

The guard should fail closed:

- every expected mount is a real mount: `/mnt/cache1`, `/mnt/data1`, `/mnt/data2`, `/mnt/parity1`, `/mnt/snapraid-content/disk1`, `/mnt/snapraid-content/disk2`, `/mnt/storage`, `/mnt/storage_slow`, `/mnt/storage_cache`;
- `/mnt/storage`, `/mnt/storage_slow`, `/mnt/storage_cache` have `fstype=fuse.mergerfs`;
- all expected mergerfs branches are present, not just the union mount. A robust way is to put a unique hidden marker file on each branch (for example `/mnt/data1/.branch-data1`) and require the corresponding entries to be visible in the union root;
- the source path of an `rclone sync` is non-empty and contains a known sentinel;
- on failure, exit non-zero and ping the Healthchecks `/fail` URL.

This one guard also covers the boot-ordering risk: `/mnt/storage` is mounted from the wildcard source `/mnt/cache*:/mnt/data*`, and systemd cannot infer dependencies on the individual data mounts from that wildcard. If a data mount is late, the union can come up without that branch.

#### P0-2. SnapRAID mass-deletion protection is disabled

**Evidence**

`host_vars/catafalco/vars.yml:311`:

```yaml
snapraid_deletethreshold: -1
```

The earlier inspection showed a run with roughly 248,000 removals being synced.

**Why it matters**

The guard is exactly what prevents an accidental `rm -rf`, a failed restore, or a missing mount from being written into parity. With `-1` it does nothing.

**Recommended fix**

Set a finite value, for example:

```yaml
snapraid_deletethreshold: 5000
```

or 10000 if the normal daily churn is higher. If the next run legitimately exceeds it:

1. inspect `snapraid diff` and the list of removals;
2. confirm they are expected (for example old InfluxDB files);
3. only then run once with `--ignore-deletethreshold`.

Do not leave it at `-1` just because one run has a large legitimate deletion. Fix the source of the churn (see P0-4) and keep the guard.

#### P0-3. Uncache can move active or incomplete downloads

**Evidence**

- The uncache script scans every file under the cache and only excludes `docker-volumes-cache` (`roles/mergerfs/tasks/main.yml:35`).
- It has no minimum age, no open-file check, and no per-path exclusion for Deluge or slskd.
- It runs when cache usage is above 75 %, and the cache is currently around 67–71 %.

**Why it matters**

If the script runs while a torrent or slskd download is active, it can move an incomplete file to the slow pool and remove the cache copy. Deluge/slskd then lose the path/inode they were writing to, which looks exactly like “the download disappeared”. It can also move files that are actively seeding, depending on their access time.

**Recommended fix**

- Exclude `{{ cache_root }}/Downloads/slskd_incomplete` and Deluge’s incomplete/active directories.
- Either exclude all of `{{ cache_root }}/Downloads` while seeding matters, or skip files modified/accessed in the last 24 h.
- Check the `rsync` return code and stop on failure.
- Add `flock` so uncache cannot overlap SnapRAID, rclone, or another uncache run.

#### P0-4. InfluxDB backup copies the whole metrics store

**Evidence**

- `roles/influxdb/tasks/main.yml:323` backs up `{{ docker_dir }}/influxdb`, i.e. `/srv/influxdb`.
- `roles/influxdb/defaults/main.yml` sets `influxdb_store_host_dir: "{{ docker_dir }}/influxdb/db"`.
- Therefore the daily `rsync --delete` copies the InfluxDB store (db/WAL/catalog/object files) into `docker-volumes-backup`, then rclone mirrors it to julia.

**Why it matters**

- It is the most likely source of the ~248k removals in the SnapRAID diff (old parquet files being pruned by retention).
- It creates a large daily write load and wakes the mechanical disks for data you explicitly do not consider important.
- It makes the service backup much larger than it needs to be.

**Recommended fix**

Back up only the InfluxDB configuration, not the store. For example:

- change the InfluxDB backup source to the specific config files/directories (`admin-token.json`, `explorer`, compose files), or
- add an exclusion for `db/` in the generic backup invocation for this service.

If metrics history is ever valuable, use InfluxDB’s own export/backup mechanism on a separate, less frequent schedule — not a raw `rsync` of the live store.

#### P0-5. Rclone mirrors still need deletion caps

`rclone sync` makes julia identical to catafalco. Julia’s weekly media snapshot (retention 1) and containers snapshots (14 today) reduce the impact, but they do not make the sync safe:

- a mass deletion on catafalco propagates to julia in one run;
- the single media snapshot can be rotated away before you notice;
- a missing mergerfs branch can look like a mass deletion.

Recommended changes, in order of simplicity:

1. Add `--max-delete` to every `rclone sync` job, including julia→catafalco Documents/Pictures, so a source-side accident cannot delete thousands of files in one run.
2. For media, consider `rclone copy` instead of `sync` (deleted files remain on julia until manually pruned), or `rclone sync --backup-dir julia:media-catafalco-versions/$(date +\%F) --max-delete 100`.
3. Ensure the julia snapshot schedule runs before the rclone sync, so the “before” state is captured.

Given the media RPO you stated (up to one month), one weekly snapshot plus a deletion cap is probably enough; a more elaborate version history is optional.

### P1 — reduce exposure and make the existing history reliable

#### P1-1. Cache-only data remains unprotected

The cache holds a large fraction of the recent data (about 618 GB used at the time of review). It is outside SnapRAID by design, has no btrfs snapshots, and is only covered indirectly by julia/B2 copies.

After the P0-3 exclusions, the remaining cache contents still have no parity. That is acceptable for transient downloads and for data that already has a julia/B2 copy, but it should be treated as a staging area, not as a protected copy.

Recommended:

- Lower the uncache target from 75 % to 60–65 % so cache-only data is demoted sooner.
- Keep the uncache job out of the Sunday SnapRAID window.
- For anything that must survive a cache failure, make sure a second copy exists before relying on it.

#### P1-2. Service backups are versioned on julia, but only locally

**Evidence**

- `files/generic_backup.sh:95` uses `rsync -avh --delete` into one catafalco destination.
- `roles/rclone/tasks/main.yml:66` then mirrors that single catafalco copy to julia with `rclone sync`.
- Julia keeps smart-retention snapshots of the containers share — 14 snapshots today.

**Why it matters**

This is less severe than it first appeared: julia snapshots do provide real version history, so one bad config does not immediately destroy every previous copy. The remaining gaps are:

- the snapshots are local-only (no offsite copy);
- the catafalco copy itself is still a single overwritten copy;
- the retention window and snapshot timing relative to the 08:50 rclone sync should be verified;
- restoring a service config from a julia snapshot has not been tested.

**Recommended fix**

- Keep the julia snapshots and verify they cover the containers path and are taken before the daily rclone sync.
- Add an offsite copy only if you want protection against losing julia as well. The data is small (few GB after P0-4), so B2/restic/Duplicati would cost very little.
- Test restoring one service config from a julia snapshot.
- Exclude the InfluxDB `db/` store (P0-4) so this backup stays small.

#### P1-3. Snapshot coverage is uneven by design

The current `snapraid-btrfs` cleanup keeps only the latest synced snapshot on catafalco. If a file is deleted on Monday, the previous Sunday snapshot can recover it until the next Sunday sync; after that, the old snapshot is removed and parity has been updated with the deletion.

Julia is different:

- media: weekly snapshot, retention 1;
- containers: smart retention, 14 snapshots today.

That means the catafalco snapshots are a frozen view for `scrub`/`check`/`fix`, not an archive, while julia provides the real deletion history for media and service configs. If you later decide that catafalco itself needs more history, add an independent weekly/monthly snapper schedule or a separate restic/Duplicati job. Do not enable hourly timeline snapshots just for this: it would wake the disks and create unnecessary churn.

#### P1-4. Daily maintenance is fragmented and hard on the disks

Current pattern:

- 02:00/02:30 Duplicati
- 04:00/05:00 many service backups (many at exactly 05:00, including stopping containers)
- 07:00 uncache (Mon–Sat)
- 08:00–08:50 rclone Documents/Pictures/docker-volumes
- Sunday 03:00 SnapRAID sync/scrub
- Sunday 12:00 media rclone

This causes several wake windows per day and some overlap risk: the 08:50 docker-volumes rclone assumes all 05:00 backups have finished. If the InfluxDB store copy is large, that may not be true.

Recommended:

- Group jobs into one or two maintenance windows.
- Run rclone only after the backups have completed (or use a lock/Healthchecks dependency).
- Keep the weekly SnapRAID sync, but avoid starting it while Duplicati or a large backup is still running.
- Measure actual sleep behaviour before optimizing further. The `hd_idle_parser.py` is not deployed, so there is currently no data to prove the disks sleep as intended.

### P2 — resilience and verification

#### P2-1. Restore tests

An untested backup is not a backup. Do a small restore drill:

- one file from Duplicati (Documents and Pictures);
- one file from julia, including a file that was deleted before the last snapshot;
- one service config from a julia snapshot of the containers share;
- ideally one file rebuilt from SnapRAID parity (`snapraid fix`) in a scratch location.

Record the result and repeat periodically.

#### P2-2. Alerting

Healthchecks pings exist for SnapRAID, rclone, and uncache, but verify that:

- the Healthchecks project actually has notification channels configured;
- a failed/missed ping alerts you;
- rclone failures are visible (the current `&& curl` only pings on success, which is correct only if Healthchecks is set to alert on missed pings);
- Duplicati failures are monitored independently.

#### P2-3. Zero sub-second timestamps

47,513 files have a zero sub-second timestamp. This is not a data-loss risk, but it makes diffs noisy. A one-time `snapraid touch` during a maintenance window would clean it up. Do not enable `snapraid_touch` permanently unless you accept the extra weekly I/O.

#### P2-4. Second parity disk

A second parity disk tolerates two simultaneous data-disk failures. It does **not** protect against deletion, ransomware, corruption, or a missing mount. Given:

- Documents/Pictures already have julia + B2;
- Media is replaceable and has a julia copy;
- 18 TB disks are expensive;

a second parity disk is **not the first place to spend money**. Fix the guards and versioning first. Revisit second parity only if the media library’s manually produced files become irreplaceable, or if disk failure during a rebuild is a concern you are willing to pay for.

#### P2-5. Boot ordering of the mergerfs union

The union is mounted from `/mnt/cache*:/mnt/data*`. Systemd cannot infer that the union depends on `/mnt/data1` and `/mnt/data2` from that wildcard. If the union starts before a data mount, it may come up with fewer branches. The preflight guard from P0-1 will catch this at job time; adding explicit `x-systemd.requires-mounts-for=` options for the data/cache mounts would make it less likely at boot.

---

## 6. What I would do, in order

### This week (P0)

1. Add the shared storage preflight guard and wire it into uncache, rclone, and the SnapRAID runner.
2. Set `snapraid_deletethreshold` to a finite value (5000–10000).
3. Harden the uncache script: exclusions for incomplete/seeding files, minimum age, `rsync` return-code check, `flock`.
4. Stop backing up the InfluxDB `db/` store; keep only its configuration.
5. Add `--max-delete` to all `rclone sync` jobs.

### Next (P1)

6. Lower the uncache target to 60–65 % once the exclusions are in place.
7. Verify julia snapshot retention/timing for the containers path, and test restoring one service config.
8. Decide whether service configs need an offsite copy; if yes, add a small restic/Duplicati job.
9. Decide whether media needs more than the current weekly snapshot (retention 1) plus `--max-delete`.
10. Consolidate maintenance windows and make rclone wait for backup completion.

### Later (P2)

11. Restore drills and alerting verification.
12. Deploy the hd-idle parser if you want to prove/optimize disk sleep.
13. One-time `snapraid touch` for the zero-timestamp files.
14. Re-evaluate second parity only after the above.

---

## 7. Open questions / information I could not verify

These do not block the P0 fixes, but they affect the final design:

1. **What is julia’s usable capacity and current free space?** It holds the Documents/Pictures mirror, the Media mirror, and the docker-volumes copy. If Media grows, julia may fill.
2. **Exact per-directory usage on cache1.** The MCP tools expose filesystem totals, not directory sizes. The earlier inspection suggested roughly 557 GB Media, 7.4 GB Pictures, 9.7 MB Documents, 7.2 GB docker-volumes-backup, but this should be re-measured.
3. **Exact snapraid-btrfs snapshot list.** `sudo snapper -c data01 list` and `sudo snapper -c data02 list` would confirm how many snapshots exist and their ages.
4. **What exactly does julia’s smart retention keep for the containers share, and when are the snapshots taken relative to the 08:50 rclone sync?** This determines whether a bad config is recoverable.
5. **Are the Healthchecks notification channels actually configured?** The pings exist; the alerting path should be tested.

Useful read-only commands to fill the gaps:

```bash
sudo findmnt -T /mnt/storage -o TARGET,SOURCE,FSTYPE,OPTIONS
sudo findmnt -T /mnt/storage_slow -o TARGET,SOURCE,FSTYPE,OPTIONS
sudo findmnt -T /mnt/storage_cache -o TARGET,SOURCE,FSTYPE,OPTIONS
sudo findmnt -T /mnt/data1 -o TARGET,SOURCE,FSTYPE,OPTIONS
sudo findmnt -T /mnt/data2 -o TARGET,SOURCE,FSTYPE,OPTIONS

sudo du -x -h --max-depth=1 /mnt/cache1 2>/dev/null | sort -h
sudo du -x -h --max-depth=1 /mnt/storage/docker-volumes-backup 2>/dev/null | sort -h

sudo snapper -c data01 list
sudo snapper -c data02 list
sudo snapper -c data01 get-config | grep -E 'NUMBER_LIMIT|TIMELINE_LIMIT|TIMELINE_CREATE'

sudo systemctl list-timers 'snapraid*' 'snapper*'
sudo crontab -l -u root
crontab -l

rclone --config /srv/rclone/rclone.conf about julia:
rclone --config /srv/rclone/rclone.conf lsd julia:
```

---

## 8. Bottom line

The current data is intact and the disks are healthy. The architecture is reasonable for the stated goals: cache-first mergerfs, SnapRAID for the bulk data, julia/B2 for the important data, weekly media mirror with a one-week snapshot.

The dangerous part is that several automation jobs treat **“not visible” as “deleted”**: a missing mount can poison the julia mirror, the uncache script can move active downloads, and SnapRAID currently has no mass-deletion guard. The InfluxDB backup also creates unnecessary daily churn. Those are fixable with small, low-risk changes:

- mount guards before every data-moving job;
- a finite SnapRAID deletion threshold;
- uncache exclusions and a lock;
- `--max-delete` on every rclone sync;
- excluding the InfluxDB store from the service backup.

Service configs already have local version history through julia snapshots; an offsite copy is optional and cheap, not urgent. The accepted one-way julia→catafalco behaviour for Documents/Pictures is fine as long as it is documented.

A second parity disk is not the first thing to buy. The first thing to buy, if anything, is a few GB of offsite storage for service configs.

---

## 9. Implementation status (2026-11-05)

The P0 findings are implemented on `feat/review-backup` (the remediation track, see
[`docs/backup/backup-plan.md`](backup/backup-plan.md)) and integrated into `fix/storage-guards`
with two additions. **Nothing has been applied to the host**, so sections 2–5 still describe the
running system until the roles are deployed.

| Finding | Commit(s) | Implementation |
|---|---|---|
| P0-1 missing mounts cascade into deletion | `b1ce734`, `ed64a0a` | Role `storage_preflight` installs `/usr/local/sbin/storage-preflight`: a Python guard that checks every data/cache/parity mount, the SnapRAID content subvolumes, and each mergerfs union's branches through hidden marker files written on the branch roots (`.branch-data1`, …). SnapRAID runs with `nohidden`, so markers never enter parity. Called under the shared `storage_maintenance_lock_file` by the uncache job, every `rclone` job and `snapraid-btrfs-runner.service` (`ExecStartPre=`); `--source` also refuses an empty source directory. A refused run pings Healthchecks `/fail`. |
| P0-2 SnapRAID mass-deletion guard disabled | `8946fdd` | `snapraid_deletethreshold: 10000` (was `-1`), plus `notify: Reload systemd` and `daemon_reload: true` so the changed unit is actually picked up (`7fb5904`). |
| P0-3 uncache can move active downloads | `ed64a0a` | `--min-age-hours 24`, `os.path.ismount` checks on cache and slow pool, non-zero `rsync` exit aborts with a `/fail` ping, and the cron job takes the shared lock with `flock -n`. Client-owned data stays in the cache: the excluded set is `docker-volumes-cache` **and the whole `Downloads` tree**, which is more conservative than the per-directory exclusions suggested above. |
| P0-4 InfluxDB backup copies the whole store | `e0ce7e0` | `influxdb_backup_exclude_paths` (`/db/`) passed to `generic_backup.sh`, which gained `--exclude` and applies `--delete-excluded` when exclusions are present, so the stale store copy is removed on the next run. The container is no longer stopped. The same commit fixes the retention check: `influxdb3 show retention` exits 0 with an empty table for a missing database, so the old existence test skipped creation and let Telegraf recreate the database with infinite retention. |
| P0-5 rclone mirrors need a deletion cap | `6558403`, `e6ba3d6` | `--max-delete` (default 100) on every `rclone sync`, julia→catafalco included. The effective value is read at run time through `/usr/local/bin/rclone-delete-cap`, so `rclone_max_delete_override_file` (`/etc/rclone-max-delete`) raises it for one cleanup and deleting the file revokes it; the runbook is in [`docs/backup/backup-plan.md`](backup/backup-plan.md) §12. |
| (P0-1, backup side) | `34261c0` | `generic_backup.sh` resolves the nearest existing ancestor of the destination and refuses unless it is `fuse.mergerfs`, so a backup can no longer fill `/` while the pool is unmounted (the backup-plan B-04 destination assertion). |

**Why marker files instead of parsing the union source.** The first implementation read
`findmnt -o SOURCE` for the union and expanded `*` globs. That cannot detect a union mounted
before a disk appeared: the kernel keeps the glob the union was mounted with (`/mnt/data*`), so
re-expanding it later always lists today's branches. Marker files test what is actually visible
through the union, which is the property that matters. Verified by experiment before adopting it.

### Behaviour changes you will notice

- **A deliberate mass deletion now stops instead of propagating.** More than 10000 removed files
  between two SnapRAID syncs aborts the run; more than 100 entries between two rclone runs aborts
  the mirror. A large cleanup is a reviewed one-off: delete the same subtree at the destination,
  or raise `/etc/rclone-max-delete` for one run and remove it afterwards
  (backup-plan §12); SnapRAID takes `--ignore-deletethreshold` after `snapraid diff`.
- **The uncache job is less aggressive.** Files touched in the last 24 h never move and the whole
  `Downloads` tree is left alone, so the cache may sit at its 75 % target with fresh data still
  in it. Lower `uncache_min_age_hours` or narrow the exclusions if that becomes a space problem.
- **Jobs can now refuse to start**, by design: missing mount/branch, empty mirror source,
  destination not on the pool. Nothing needs to be configured; a refusal appears as a failed
  Healthchecks ping and a cron error.
- **InfluxDB is no longer stopped nightly**, and its backup directory shrinks to configuration.
- The shared maintenance lock serialises uncache, rclone and SnapRAID: a job that finds the lock
  held exits instead of overlapping.

### Deploy

```bash
ansible-playbook catafalco.yml -K --tags "mounts,mergerfs,storage-preflight,snapraid,rclone,influxdb"
```

The guard role runs after `mounts` because it writes the branch markers onto the mounted
branches. Deploying the runner before the guard makes the SnapRAID unit fail closed, which is
visible but noisy; use the tag list above.

### One-off migrations, in this order

The first run after the deploy removes the InfluxDB store copy from
`docker-volumes-backup`. That is tens of thousands of files, which trips both new limits by
design. Do it deliberately rather than waiting for the timers:

```bash
# 1. guard sees the real array, markers are visible through every union
sudo /usr/local/sbin/storage-preflight
ls -a /mnt/storage/.branch-* /mnt/storage_slow/.branch-* /mnt/storage_cache/.branch-*

# 2. run the InfluxDB backup once; watch the store copy disappear
sudo /usr/local/sbin/generic_backup.sh \
  --sources /srv/influxdb --dest /mnt/storage/docker-volumes-backup --exclude "/db/"
du -sh /mnt/storage/docker-volumes-backup/influxdb

# 3. the julia mirror must delete those entries; higher cap, reviewed first
rclone --config /srv/rclone/rclone.conf delete julia:catafalco/containers/influxdb --dry-run
rclone --config /srv/rclone/rclone.conf delete julia:catafalco/containers/influxdb

# 4. SnapRAID now sees those removals
sudo /usr/local/bin/snapraid diff
# if the removals are only the influxdb store and exceed 10000, run once with:
sudo /usr/bin/python3 /usr/local/share/snapraid-btrfs-runner/snapraid-btrfs-runner.py \
  -c /usr/local/share/snapraid-btrfs-runner/snapraid-btrfs-runner.conf --ignore-deletethreshold
```

Step 3 removes the julia copy directly instead of raising `--max-delete`, because the data is
being retired on purpose; julia's snapshots still hold the previous state.

### What was verified, and what was not

Verified in a non-root sandbox (real kernel mount table in a private user+mount namespace, no
Python available):

- The `generic_backup.sh` destination check: destination on a mergerfs-like mount passes, a
  non-mergerfs/`/` mount refuses before any container is stopped, a not-yet-created destination
  resolves to its nearest existing ancestor, and a missing `findmnt` refuses.
- The `--exclude`/`--delete-excluded` construction with a recording `rsync` shim, matching the
  InfluxDB invocation (`--exclude /db/`), file sources, and failure propagation.
- The marker-based branch semantics were confirmed against the design: a union records the glob
  it was mounted with, so source parsing cannot see a branch that appeared later — this is why
  markers are used.
- `flock -w` / lock-contention behaviour.

Not executable in the sandbox (no `python3`, `ansible`, `rsync`, `rclone`, `mergerfs`): the guard
and uncache scripts themselves, and the playbook. Before the first real run:

```bash
python3 -m py_compile /usr/local/sbin/storage-preflight /usr/local/bin/mergerfs-uncache
sudo /usr/local/sbin/storage-preflight; echo $?            # 0 on a healthy array
sudo umount /mnt/data2 && sudo /usr/local/sbin/storage-preflight; echo $?   # 1
sudo mount /mnt/data2
```

Also run `pre-commit run --all-files` (including `ansible-lint`) on a machine that has Python;
the commits here could not run the hooks.

### Still open

From [`docs/backup/backup-plan.md`](backup/backup-plan.md), still relevant to storage safety:
**B-P0-3** (`generic_backup.sh` can leave containers stopped: `set -e` and no restart trap),
**B-P0-4** (the restore path is untested and deletes; keep `docker_restore_volumes_backup` off),
**B-P0-5** (non-atomic `latest.sql.gz` dumps), **B-03** (grafana/swag/stirling-pdf have no jobs),
**B-08** (backup freshness monitoring). From this review: uncache target 60–65 %, julia snapshot
retention/timing, offsite copy for service configs, restore drills, `hd_idle_parser.py`,
one-off `snapraid touch`, boot-order hint (`x-systemd.requires-mounts-for=`). A second parity
disk remains the last thing to buy.
