#!/usr/bin/env python3
"""Read-only SQLite online backup and private snapshot verification.

The source is opened with SQLite's URI ``mode=ro`` flag and the destination is
created separately.  This uses the SQLite online backup API, so committed WAL
frames are part of the pinned source snapshot; it never copies a live main
file, WAL, or SHM sidecar and never writes to the source database.
"""

import argparse
import os
import sqlite3
import stat
import sys
from urllib.parse import quote


def fail(message):
    print(f"error: {message}", file=sys.stderr)
    return 1


def regular(path, label):
    if os.path.islink(path):
        raise ValueError(f"{label} must not be a symlink")
    info = os.stat(path)
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{label} must be a regular file")
    return info


def ro_uri(path):
    return "file:" + quote(os.path.abspath(path), safe="/") + "?mode=ro"


def verify_database(path, expected_tables):
    regular(path, "database")
    for suffix in ("-wal", "-shm"):
        if os.path.lexists(path + suffix):
            raise ValueError("snapshot has an unexpected SQLite sidecar")
    connection = sqlite3.connect(ro_uri(path), uri=True, isolation_level=None)
    try:
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        result = connection.execute("PRAGMA integrity_check").fetchone()
        if not result or result[0] != "ok":
            raise ValueError("snapshot integrity check failed")
        for table in expected_tables:
            found = connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                (table,),
            ).fetchone()
            if not found:
                raise ValueError("snapshot schema is missing an expected table")
    finally:
        connection.close()


def backup(source, destination):
    source = os.path.abspath(source)
    destination = os.path.abspath(destination)
    if source == destination:
        raise ValueError("source and destination must differ")
    source_info = regular(source, "source database")
    parent = os.path.dirname(destination)
    if not os.path.isdir(parent) or os.path.islink(parent):
        raise ValueError("destination parent must be a real directory")
    if os.path.lexists(destination):
        raise ValueError("destination already exists")
    partial = destination + ".partial"
    if os.path.lexists(partial):
        raise ValueError("incomplete destination already exists")

    old_umask = os.umask(0o077)
    source_connection = None
    target_connection = None
    try:
        source_connection = sqlite3.connect(
            ro_uri(source), uri=True, isolation_level=None
        )
        source_connection.execute("PRAGMA query_only=ON")
        source_connection.execute("BEGIN")
        target_connection = sqlite3.connect(partial)
        source_connection.backup(target_connection, pages=128, sleep=0.01)
        # A WAL-mode source can transfer that journal mode to the target. Make
        # the restored artifact a standalone main file before closing it; this
        # writes only the new private destination, never the source.
        target_connection.execute("PRAGMA journal_mode=DELETE")
        target_connection.commit()
        target_connection.close()
        target_connection = None
        verify_database(partial, ("machines", "tasks"))
        os.chmod(partial, stat.S_IMODE(source_info.st_mode), follow_symlinks=False)
        if hasattr(os, "chown"):
            os.chown(
                partial,
                source_info.st_uid,
                source_info.st_gid,
                follow_symlinks=False,
            )
        os.replace(partial, destination)
        print("sqlite online backup complete; source opened read-only")
    except Exception as exc:
        if target_connection is not None:
            target_connection.close()
        raise ValueError("SQLite online backup failed") from exc
    finally:
        if source_connection is not None:
            source_connection.close()
        os.umask(old_umask)


def main():
    parser = argparse.ArgumentParser(add_help=False)
    subparsers = parser.add_subparsers(dest="operation")
    backup_parser = subparsers.add_parser("backup")
    backup_parser.add_argument("--source", required=True)
    backup_parser.add_argument("--destination", required=True)
    verify_parser = subparsers.add_parser("verify")
    verify_parser.add_argument("--database", required=True)
    verify_parser.add_argument("--expect-table", action="append", default=[])
    args = parser.parse_args()
    try:
        if args.operation == "backup":
            backup(args.source, args.destination)
        elif args.operation == "verify":
            verify_database(args.database, args.expect_table)
            print("sqlite snapshot integrity and schema verified")
        else:
            return fail("operation must be backup or verify")
    except (OSError, sqlite3.Error, ValueError):
        return fail("SQLite snapshot validation failed")
    return 0


if __name__ == "__main__":
    sys.exit(main())
