#!/usr/bin/env python3
"""Destination metadata preflight and sealed-plan directory operations.

No credential contents are read. `plan`/`report` are read-only. `record` writes
only into the fresh backup; `apply` is called by cutover after that backup seals.
Existing rollback DIRECTORY/DIR_ABSENT rows restore changed directory metadata.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import stat
import sys


def metadata(path):
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return {"state": "absent"}
    kind = "directory" if stat.S_ISDIR(info.st_mode) else "file" if stat.S_ISREG(info.st_mode) else "unsafe"
    return {"state": kind, "mode": format(stat.S_IMODE(info.st_mode), "04o"),
            "uid": info.st_uid, "gid": info.st_gid, "dev": info.st_dev,
            "ino": info.st_ino, "nlink": info.st_nlink}


def check_ancestors(path):
    """Reject every symlink component, including dangling links; never resolve it away."""
    path = Path(path)
    if not path.is_absolute() or os.path.normpath(path) != str(path) or any(c in str(path) for c in "\t\n\r"):
        raise ValueError(f"noncanonical absolute path required: {path}")
    for parent in reversed(path.parents):
        info = metadata(parent)
        if info["state"] == "absent":
            continue
        if info["state"] != "directory":
            raise ValueError(f"non-directory/symlink ancestor: {parent}")
        if info["uid"] not in (0, os.getuid()):
            raise ValueError(f"foreign-owned ancestor: {parent}")
        mode = int(info["mode"], 8)
        if mode & 0o002 and not mode & stat.S_ISVTX:
            raise ValueError(f"other-writable non-sticky ancestor: {parent}")


def make_plan(paths):
    uid = os.getuid()
    plan = {"format": "agent-board-filesystem/1", "paths": paths,
            "destinations": [], "directories": [], "errors": [], "pending": []}
    directories = {}

    def directory(path, private=False, create=True):
        path = str(path)
        check_ancestors(path)
        info = metadata(path)
        if info["state"] not in ("directory", "absent"):
            raise ValueError(f"directory is not real (including symlink): {path}")
        if info["state"] == "directory" and info["uid"] != uid:
            raise ValueError(f"destination parent must be owned by operator: {path}")
        action = "create0700" if info["state"] == "absent" else "harden0700" if private and info["mode"] != "0700" else "keep"
        if info["state"] == "absent":
            if not create:
                raise ValueError(f"parent must already exist: {path}")
            directory(Path(path).parent)
        elif action == "keep" and not os.access(path, os.W_OK | os.X_OK):
            raise ValueError(f"destination parent is not writable/searchable: {path}")
        prior = directories.get(path)
        if prior and prior["action"] == "harden0700":
            action = prior["action"]
        directories[path] = {"path": path, **info, "action": action}

    required = {"unit", "binary", "env", "db", "candidate", "record"}
    fresh = {"credential", "endpoint", "evidence", "backup"}
    private_parent = {"auth", "credential", "endpoint"}
    for label, path in paths.items():
        if not path:
            continue
        try:
            check_ancestors(path)
            info = metadata(path)
            plan["destinations"].append({"label": label, "path": path, **info})
            if label == "backup":
                if info["state"] != "absent":
                    raise ValueError(f"backup must be a fresh absent path: {path}")
                # It is retained evidence, never hardened/removed by live rollback.
                directory(Path(path).parent, create=False)
                continue
            if label == "artifacts":
                directory(path, private=True)
                continue
            if info["state"] == "unsafe" or info["state"] == "directory":
                raise ValueError(f"not a regular file (including symlink): {path}")
            if label in fresh and info["state"] != "absent":
                raise ValueError(f"new destination already exists: {path}")
            if info["state"] == "file":
                if info["uid"] != uid or info["nlink"] != 1:
                    raise ValueError(f"file must be operator-owned with one link: {path}")
                if label in {"auth", "staged", "env"} and info["mode"] != "0600":
                    raise ValueError(f"protected file must be mode0600: {path}")
                if label in {"binary", "candidate"} and not os.access(path, os.X_OK):
                    raise ValueError(f"binary is not executable: {path}")
            elif label in required:
                raise ValueError(f"required input absent: {path}")
            if label in {"candidate", "staged"}:
                # Inputs are read-only; staging is prepared separately by bootstrap.
                parent = metadata(Path(path).parent)
                if parent["state"] != "directory":
                    if label == "staged":
                        plan["pending"].append(f"bootstrap must create fresh private staging: {Path(path).parent}")
                    else:
                        raise ValueError(f"input parent absent: {path}")
                elif parent["uid"] != uid or (label == "staged" and parent["mode"] != "0700"):
                    raise ValueError(f"input parent owner/private mode invalid: {Path(path).parent}")
                if label == "staged" and info["state"] == "absent":
                    plan["pending"].append(f"staged auth absent; protected bootstrap required: {path}")
                continue
            directory(Path(path).parent, private=label in private_parent,
                      create=label in {"auth", "credential", "endpoint", "evidence"})
        except (OSError, ValueError) as error:
            plan["errors"].append(str(error))

    if metadata(paths["auth"])["state"] == "absent" and not paths.get("staged"):
        plan["errors"].append("auth absent: --staged-auth is required")
    temporaries = [("env", ".tmp"), ("auth", ".participant.tmp"),
                   ("credential", ".tmp"), ("endpoint", ".tmp")]
    for label, suffix in temporaries:
        if paths.get(label) and os.path.lexists(paths[label] + suffix):
            plan["errors"].append(f"temporary destination already exists: {paths[label] + suffix}")
    for suffix in ("-wal", "-shm"):
        sidecar = paths["db"] + suffix
        info = metadata(sidecar)
        if info["state"] != "absent" and (info["state"] != "file" or info["uid"] != uid):
            plan["errors"].append(f"unsafe database sidecar: {sidecar}")
    named = [p for p in paths.values() if p]
    if len(named) != len(set(named)):
        plan["errors"].append("input/destination paths must be distinct")
    for label in ("credential", "endpoint", "evidence", "auth", "unit", "env", "binary", "db", "record", "staged"):
        if paths.get(label):
            for container in ("backup", "artifacts"):
                if Path(paths[label]).is_relative_to(paths[container]):
                    plan["errors"].append(f"{label} must not be inside {container}")
    plan["directories"] = sorted(directories.values(), key=lambda row: (len(Path(row["path"]).parts), row["path"]))
    plan["status"] = "blocked" if plan["errors"] else "pending_bootstrap" if plan["pending"] else "validated_plan"
    return plan


def validate_unchanged(plan):
    if plan["format"] != "agent-board-filesystem/1" or plan["status"] != "validated_plan":
        raise ValueError("only a fully validated filesystem plan may be applied")
    for row in plan["directories"]:
        check_ancestors(row["path"])
        now = metadata(row["path"])
        # Directory link count can change when the retained backup is created.
        for key in ("state", "mode", "uid", "gid", "dev", "ino"):
            if row.get(key) != now.get(key):
                raise ValueError(f"directory changed since preflight: {row['path']}")


def record(plan, backup):
    validate_unchanged(plan)
    target = Path(backup)
    if str(target.absolute()) != plan["paths"]["backup"]:
        raise ValueError("backup does not match preflight destination")
    with (target / "filesystem-plan.json").open("x") as handle:
        json.dump(plan, handle, sort_keys=True, indent=2)
        handle.write("\n")
    os.chmod(target / "filesystem-plan.json", 0o600)
    # Append only affected directories, deepest first AFTER all file rows.
    # Keep directories in the audit plan but never chmod unchanged service parents.
    with (target / "manifest.meta").open("a") as handle:
        for row in reversed(plan["directories"]):
            if row["action"] == "keep":
                continue
            if row["state"] == "absent":
                handle.write(f"DIR_ABSENT\t-\t-\t-\t{row['path']}\t-\t-\n")
            else:
                handle.write(f"DIRECTORY\t{row['mode'].lstrip('0') or '0'}\t{row['uid']}\t{row['gid']}\t{row['path']}\t-\t-\n")


def apply(plan, plan_file):
    if not plan_file:
        raise ValueError("apply requires a sealed plan file")
    path = Path(plan_file)
    info = metadata(path)
    if path.name != "filesystem-plan.json" or info["state"] != "file" or info["uid"] != os.getuid() or info["mode"] != "0600":
        raise ValueError("apply requires an owned protected plan file")
    backup = path.parent
    if str(backup.absolute()) != plan["paths"]["backup"]:
        raise ValueError("plan backup path mismatch")
    seal = dict(line.split("=", 1) for line in (backup / "backup.complete").read_text().splitlines())
    for name, key in (("manifest.sha256", "manifest_sha256"), ("manifest.meta", "manifest_meta_sha256")):
        if hashlib.sha256((backup / name).read_bytes()).hexdigest() != seal[key]:
            raise ValueError("filesystem plan backup seal mismatch")
    entries = dict(line.split("  ", 1)[::-1] for line in (backup / "manifest.sha256").read_text().splitlines())
    if entries.get(path.name) != hashlib.sha256(path.read_bytes()).hexdigest():
        raise ValueError("filesystem plan is not bound to the backup seal")
    validate_unchanged(plan)
    for row in plan["directories"]:
        path = row["path"]
        check_ancestors(path)
        if row["action"] == "create0700":
            os.mkdir(path, 0o700)
        elif row["action"] == "harden0700":
            # No recursive chmod or chown: wrong owners are a preflight blocker.
            os.chmod(path, 0o700, follow_symlinks=False)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("plan", "report", "record", "apply"))
    for name in ("unit", "binary", "env", "db", "candidate", "auth", "staged", "record", "evidence", "credential", "endpoint", "artifacts", "backup"):
        parser.add_argument("--" + name, default="")
    parser.add_argument("--plan-file")
    args = parser.parse_args()
    if args.operation == "plan":
        paths = {key: value for key, value in vars(args).items() if key not in {"operation", "plan_file"}}
        plan = make_plan(paths)
        print(json.dumps(plan, sort_keys=True, indent=2))
        return 1 if plan["errors"] else 2 if plan["pending"] else 0
    plan = json.loads(Path(args.plan_file).read_text()) if args.plan_file else json.load(sys.stdin)
    if args.operation == "report":
        print("destination filesystem preflight (read-only): " + plan["status"])
        for row in plan["destinations"]:
            print(f"  {row['label']}: {row['path']} [{row['state']} mode={row.get('mode', '-')} uid={row.get('uid', '-')} gid={row.get('gid', '-')}]")
        for row in plan["directories"]:
            print(f"  parent {row['path']}: {row.get('mode', 'absent')} uid={row.get('uid', '-')} gid={row.get('gid', '-')} -> {row['action']}")
            if row["action"] == "harden0700":
                print("    impact: remove group/other traversal/listing; owner access retained; children unchanged")
        for issue in plan["errors"] + plan["pending"]:
            print("  unmet: " + issue)
    elif args.operation == "record":
        record(plan, args.backup)
    else:
        apply(plan, args.plan_file)
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (OSError, ValueError, KeyError) as error:
        print(f"filesystem preflight refused: {error}", file=sys.stderr)
        sys.exit(1)
