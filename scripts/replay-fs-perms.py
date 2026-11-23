#!/usr/bin/env python3
"""Replay a `find -printf '%u:%g %m %p\\n'` dump onto the filesystem.

Why this exists: `snapraid fix` restores file contents, names, timestamps,
symlinks and hardlinks, but NOT ownership, permissions or xattrs. After a disk
rebuild every file comes back as root:root with the reconstructed inode's mode,
which breaks services that run as a non-root uid (Samba shares, container
volumes, Telegraf's journal buffer).

This script replays the ownership/permissions recorded before the swap. It is
idempotent and safe to re-run.

Usage:
    sudo scripts/replay-fs-perms.py /root/rma-data2/data2-perms.txt --dry-run
    sudo scripts/replay-fs-perms.py /root/rma-data2/data2-perms.txt

Notes:
  - Paths missing on disk are counted and skipped (a rebuild legitimately does
    not recreate files that were deleted before the final sync).
  - Symlinks are chowned with follow_symlinks=False and are not chmod-ed.
  - Numeric ids (e.g. container uids like 70:70) are used as-is; names are
    resolved through pwd/grp.
"""

from __future__ import annotations

import argparse
import grp
import os
import pwd
import sys


def resolve_uid(name: str) -> int | None:
    if name.isdigit():
        return int(name)
    try:
        return pwd.getpwnam(name).pw_uid
    except KeyError:
        return None


def resolve_gid(name: str) -> int | None:
    if name.isdigit():
        return int(name)
    try:
        return grp.getgrnam(name).gr_gid
    except KeyError:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("dump", help="output of find -printf '%%u:%%g %%m %%p\\n'")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would change without touching anything",
    )
    args = parser.parse_args()

    if os.geteuid() != 0:
        print("Run as root: sudo scripts/replay-fs-perms.py ...", file=sys.stderr)
        return 2

    stats = {"applied": 0, "unchanged": 0, "missing": 0, "unresolved": 0, "failed": 0}
    failures: list[str] = []

    with open(args.dump, encoding="utf-8", errors="surrogateescape") as handle:
        for line in handle:
            line = line.rstrip("\n")
            if not line:
                continue
            try:
                owner, mode, path = line.split(" ", 2)
                user, group = owner.split(":", 1)
            except ValueError:
                stats["failed"] += 1
                failures.append(f"unparsable line: {line!r}")
                continue

            if not os.path.lexists(path):
                stats["missing"] += 1
                continue

            uid = resolve_uid(user)
            gid = resolve_gid(group)
            if uid is None or gid is None:
                stats["unresolved"] += 1
                failures.append(f"unknown owner {owner} for {path}")
                continue

            st = os.lstat(path)
            want_mode = int(mode, 8)
            is_link = os.path.islink(path)
            owner_ok = st.st_uid == uid and st.st_gid == gid
            mode_ok = is_link or (st.st_mode & 0o7777) == want_mode
            if owner_ok and mode_ok:
                stats["unchanged"] += 1
                continue

            if args.dry_run:
                stats["applied"] += 1
                continue

            try:
                os.chown(path, uid, gid, follow_symlinks=False)
                if not is_link:
                    os.chmod(path, want_mode)
                stats["applied"] += 1
            except OSError as exc:
                stats["failed"] += 1
                if len(failures) < 20:
                    failures.append(f"{path}: {exc}")

    print(
        " ".join(f"{key}={value}" for key, value in stats.items()),
    )
    for line in failures[:20]:
        print(f"  {line}", file=sys.stderr)
    if args.dry_run:
        print("dry-run: nothing written", file=sys.stderr)
    return 1 if stats["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
