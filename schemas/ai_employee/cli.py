"""``ai-employee`` console entry point.

Two subcommands, both thin wrappers over :mod:`ai_employee.foundation`:

* ``new-employee`` — scaffold a fresh employee workspace.
* ``validate-foundation`` — validate an existing workspace + deployment
  record against ``employee-foundation/1.0``.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .foundation import FoundationError, scaffold_employee, validate_foundation


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="ai-employee")
    subparsers = parser.add_subparsers(dest="command", required=True)

    new_employee = subparsers.add_parser(
        "new-employee", help="scaffold a new employee workspace"
    )
    new_employee.add_argument("employee_id")
    new_employee.add_argument(
        "--root", required=True, help="workspace directory to create"
    )
    new_employee.add_argument("--display-name", required=True)
    new_employee.add_argument("--tenant-id", default="lee-installation")
    new_employee.add_argument("--principal", default="principal:lee")

    validate = subparsers.add_parser(
        "validate-foundation", help="validate an employee workspace"
    )
    validate.add_argument("workspace")
    validate.add_argument("--deployment-record", required=True)
    validate.add_argument(
        "--json", action="store_true", help="print the full report as JSON"
    )

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "new-employee":
        try:
            written = scaffold_employee(
                Path(args.root),
                args.employee_id,
                display_name=args.display_name,
                tenant_id=args.tenant_id,
                principal=args.principal,
            )
        except FoundationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        for path in written:
            print(path)
        return 0

    if args.command == "validate-foundation":
        try:
            report = validate_foundation(
                Path(args.workspace), Path(args.deployment_record)
            )
        except FoundationError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        except OSError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 2
        if args.json:
            print(json.dumps(report.to_dict(), indent=2, sort_keys=True))
        else:
            for item in report.findings:
                print(f"[{item.severity}] {item.code}: {item.path}: {item.message}")
            print("OK" if report.ok else "FAILED")
        return 0 if report.ok else 1

    parser.error(f"unknown command: {args.command!r}")
    return 2  # pragma: no cover - argparse exits before this


if __name__ == "__main__":
    raise SystemExit(main())
