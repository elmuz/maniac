# Replacing a SnapRAID data disk

Replacing a member of the mergerfs + SnapRAID array with a spare, rebuilding it
from parity. Written from the 2026-11-21 Data2 replacement (Seagate
`ST18000NT001-3NF101`, RMA), including what that run got wrong.

**The invariant that matters:** parity is a snapshot of *all* data disks at the
last sync. `snapraid fix` XORs parity against the surviving data disks, so any
write to those disks during the rebuild produces silently wrong blocks. And
`snapraid check` compares data against the content hashes, so any write **after**
the last sync produces false errors. Both operations need an array nobody else
is touching.

Measured on this host (18 TB CMR disks, `Intel` SATA): **~20 h per 4.7 TB** for
`fix`, **~6.5 h** for a full `check`. Plan the window as read-only for a day.

## 1. Back up before you touch anything

Mirror to the off-site target with `rclone copy`, not `sync`, when the source is
suspect: a mirror gives a suspect source delete authority over the backup.

```bash
sudo rclone --config /srv/rclone/rclone.conf copy <src> <remote> \
  --exclude '@Recently-Snapshot/**' --exclude '@Recycle/**' --progress
```

`rclone-delete-cap` / `--max-delete` still apply to the scheduled `sync` jobs.

## 2. Baseline

Enough to rebuild *and* to prove the rebuild afterwards:

```bash
sudo mkdir -p /root/rma-data2
sudo cp /etc/snapraid.conf /etc/fstab /root/rma-data2/
sudo cp /var/local/snapraid/snapraid.content /root/rma-data2/content-shared
sudo cp /mnt/snapraid-content/disk1/snapraid.content /root/rma-data2/content-disk1
sudo cp /mnt/snapraid-content/disk2/snapraid.content /root/rma-data2/content-disk2
sudo tar czf /root/rma-data2/snapper-configs.tgz -C /etc/snapper configs

# `sudo cmd > /root/x` writes as the caller - use tee.
sudo lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,LABEL,MOUNTPOINT,UUID \
  | sudo tee /root/rma-data2/lsblk-before.txt >/dev/null
sudo snapraid status | sudo tee /root/rma-data2/snapraid-status-before.txt >/dev/null
sudo snapraid diff   | sudo tee /root/rma-data2/snapraid-diff-before.txt >/dev/null

# `fix` restores content, not ownership: this dump is the only record.
sudo find /mnt/data2 -xdev -printf '%u:%g %m %p\n' \
  | sudo tee /root/rma-data2/data2-perms.txt | wc -l
```

Ship the baseline off-host (`sudo rclone copy /root/rma-data2 …`) so it survives
the disk and the box.

## 3. Stop every writer

`snapraid fix` does **not** take `storage_maintenance_lock_file`, so the
scheduled jobs must be off for the whole window — and *disabled*, because a
reboot brings enabled units back:

```bash
sudo systemctl stop snapraid-btrfs-runner.timer
sudo systemctl stop cron                 # mergerfs-uncache 07:00, rclone 08:00-08:50
sudo systemctl disable --now docker.socket docker smbd
sudo systemctl disable snapraid-btrfs-runner.timer
sudo systemctl stop apt-daily.timer apt-daily-upgrade.timer
```

Verify with `pgrep -a snapraid`, `pgrep -a rclone`, `systemctl is-active docker
smbd`, and later `iotop -obk` (only `snapraid` and btrfs kernel threads).

## 4. Final sync, then confirm zero

```bash
sudo /usr/local/sbin/storage-preflight --source /mnt/storage
sudo /usr/bin/flock /run/lock/storage-maintenance.lock \
  /usr/bin/python3 /usr/local/share/snapraid-btrfs-runner/snapraid-btrfs-runner.py \
  -c /usr/local/share/snapraid-btrfs-runner/snapraid-btrfs-runner.conf \
  --no-scrub --no-cleanup

sudo snapraid diff      # must be 0 added / 0 removed / 0 updated
```

`--no-scrub` avoids stressing a suspect disk; `--no-cleanup` keeps the old
snapraid snapshots as a fallback until the rebuild is verified. The runner's
Healthchecks ping fails while SWAG is down — expected, not a regression.

## 5. Prepare the spare **before** powering off

fstab mounts `LABEL=DataN` with `subvol=/data`, written without `nofail`. Boot
with a blank disk and `local-fs.target` fails → emergency mode.

```bash
lsblk -o NAME,SIZE,MODEL,SERIAL,FSTYPE,LABEL,MOUNTPOINT   # identify by serial
sudo smartctl -t short /dev/disk/by-id/<NEW>              # ~2 min
sudo wipefs -a /dev/disk/by-id/<NEW>
sudo sgdisk --clear --new=1:0:0 --typecode=1:8300 --change-name=1:Data2 /dev/disk/by-id/<NEW>
sudo mkfs.btrfs -L Data2 /dev/disk/by-id/<NEW>-part1
sudo mkdir -p /mnt/newdata2 && sudo mount /dev/disk/by-id/<NEW>-part1 /mnt/newdata2
sudo btrfs subvolume create /mnt/newdata2/data
sudo btrfs subvolume create /mnt/newdata2/content
sudo umount /mnt/newdata2 && sudo rmdir /mnt/newdata2
```

A whole-disk btrfs works too (every path is `LABEL=`/serial based); partitioning
just keeps the layout identical to the other members. If you do add `nofail` as
temporary scaffolding, it belongs in the *options* field
(`subvol=/data,nofail`) — appending it after `0 0` makes the line a 7th field and
`mount` rejects the whole file.

Update `host_vars/<host>/vars.yml`: only the disk's `model` and `serial`. The
serial is the identity — `mounts`, `smartd` and `telegraf` resolve it to exactly
one `/dev/disk/by-id` link and fail the play otherwise.

## 6. Swap, boot, Ansible

```bash
sudo umount /mnt/storage /mnt/storage_slow /mnt/storage_cache
sudo umount /mnt/data2 /mnt/snapraid-content/disk2   # never umount -l
sudo shutdown -h now
# swap the disk, boot
ansible-playbook catafalco.yml -K \
  --tags "mounts,mergerfs,storage-preflight,snapraid,telegraf,smartd"
```

**Do not run the `snapraid` tag before the new disk has valid `.snapshots`
subvolumes.** Historically that tag pulled in `snapper`, whose
`snapraid-btrfs list` probe reports a missing `.snapshots` with the same wording
as real corruption; the rescue path then deleted *every* data disk's snapshots
and snapper configs (fixed in `fix(snapper): stop the reset path from wiping
healthy disks' snapshots`, but the ordering still matters — the rescue is
destructive by design for genuinely broken subvolumes).

## 7. Rebuild from parity

```bash
sudo systemctl stop snapraid-btrfs-runner.timer   # the playbook re-arms it
sudo snapraid -d d2 -l /root/fix-d2.log fix       # tmux; ~20 h for 4.7 TB
```

Plain `snapraid`, not the `snapraid-btrfs` runner: `fix` does not use snapper.
`-d` must precede the command. `fix` does not write the content file.

A good run ends with `0 io errors`, `0 data errors`, `0 unrecoverable errors`.
The large `soft errors` / `recovered errors` counters are expected — every block
of the replaced disk was reconstructed.

## 8. Restore ownership and permissions

`snapraid fix` restores content, names, timestamps, symlinks, hardlinks — **not**
ownership, permissions or xattrs. Files come back `root:root 0600`, which breaks
Samba shares and any consumer running as another uid.

```bash
sudo scripts/replay-fs-perms.py /root/rma-data2/data2-perms.txt --dry-run
sudo scripts/replay-fs-perms.py /root/rma-data2/data2-perms.txt
```

`missing` should equal the files deleted between the baseline and the final sync
(~600 in 2026-11); they are legitimately not recreated. A blanket
`chown -R user:user` is *not* equivalent: it loses group-write bits and
mis-owns container-owned entries (`70:70 600`, `telegraf:systemd-journal 600`).
Do this before starting Docker.

## 9. Prove it: diff → sync → check, quiesced

```bash
sudo snapraid diff      # 0 differences
sudo snapraid sync      # writes the content file, including on the new disk
sudo snapraid -l /root/check-all.log check
```

- A clean `check` prints only `Everything OK`; the soft/io/data/unrecoverable
  counter block appears only when something was found.
- `-d` does **not** shorten `check` (15.1 still reads every disk): expect ~6.5 h.
- Checking a live array reports moved/rewritten files as data errors, counted per
  *block*. In 2026-11 a live check produced `2847 data errors` /
  `DANGER! Unexpected silent data errors!` from 368 changed files
  (`docker-volumes-backup` churn plus the morning `mergerfs-uncache` moving
  files onto Data1). Do not "fix" anything on such a result — quiesce and re-run.
- `check` phase errors are tagged with their disk (`error:BLOCK:DISK:FILE:…`);
  `Open error … No such file or directory` during the search phase is a file
  deleted since the last sync, not corruption.

## 10. Back to service

```bash
sudo systemctl enable --now docker.socket docker smbd
sudo systemctl enable --now snapraid-btrfs-runner.timer
sudo systemctl start cron apt-daily.timer apt-daily-upgrade.timer
```

Keep the removed disk until the quiesced `check` is clean — it is the third copy
of everything that was on it.
