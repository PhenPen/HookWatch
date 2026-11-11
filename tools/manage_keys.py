"""Create, list, rotate and revoke HookWatch operator API keys.

The keys live in the same SQLite database as the events. Only a SHA-256 hash is
stored, so the raw key is printed once, when it is created or rotated, and
cannot be recovered afterwards.

Examples:

    uv run python -m tools.manage_keys create --name hermes --role operator
    uv run python -m tools.manage_keys create --name dashboard --role viewer
    uv run python -m tools.manage_keys list
    uv run python -m tools.manage_keys list --all
    uv run python -m tools.manage_keys rotate --name hermes
    uv run python -m tools.manage_keys revoke --name dashboard

The key goes to stdout as JSON and the warning that it will not be shown again
goes to stderr, so a script can capture the secret without hiding the warning.

Exit codes:

    0  the command succeeded
    1  the database is missing, the name is unknown, or the arguments are invalid
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from pathlib import Path
from typing import Sequence

from app.db import Principal, Role, migrate
from app.repositories.keys import KeyRepository
from app.services.keys import IssuedKey, KeyService

DEFAULT_DATABASE_PATH = "hookwatch.db"


def format_issued(issued: IssuedKey) -> str:
    """One issued key, including the secret, which is why it is printed once."""
    return json.dumps(
        {
            "name": issued.principal.name,
            "role": issued.principal.role.value,
            "prefix": issued.principal.key_prefix,
            "key": issued.key,
            "created_at": issued.principal.created_at.isoformat(),
        },
        ensure_ascii=False,
    )


def format_principal(principal: Principal) -> str:
    """One key without the secret, which is all the database can produce."""
    return json.dumps(
        {
            "id": principal.id,
            "name": principal.name,
            "role": principal.role.value,
            "prefix": principal.key_prefix,
            "created_at": principal.created_at.isoformat(),
            "last_used_at": (
                principal.last_used_at.isoformat()
                if principal.last_used_at is not None
                else None
            ),
            "revoked_at": (
                principal.revoked_at.isoformat()
                if principal.revoked_at is not None
                else None
            ),
        },
        ensure_ascii=False,
    )


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="manage_keys",
        description="Create, list, rotate and revoke operator API keys.",
    )

    # --db sits on every subcommand rather than before it, so the command reads
    # naturally: manage_keys create --name x --db path.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument(
        "--db",
        default=None,
        help=f"database file (default: HOOKWATCH_DB_PATH or {DEFAULT_DATABASE_PATH})",
    )

    subparsers = parser.add_subparsers(dest="command", required=True)

    create = subparsers.add_parser(
        "create",
        parents=[common],
        help="issue a new key and print it once",
    )
    create.add_argument("--name", required=True, help="identity recorded in history")
    create.add_argument(
        "--role",
        required=True,
        choices=[role.value for role in Role],
        help="viewer can read; operator can also replay",
    )

    listing = subparsers.add_parser(
        "list",
        parents=[common],
        help="print keys without their secrets",
    )
    listing.add_argument(
        "--all",
        action="store_true",
        help="include revoked keys",
    )

    rotate = subparsers.add_parser(
        "rotate",
        parents=[common],
        help="revoke the active key and print its replacement once",
    )
    rotate.add_argument("--name", required=True, help="identity to rotate")

    revoke = subparsers.add_parser(
        "revoke",
        parents=[common],
        help="revoke the active key for a name",
    )
    revoke.add_argument("--name", required=True, help="identity to revoke")

    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    path = Path(
        args.db or os.environ.get("HOOKWATCH_DB_PATH", DEFAULT_DATABASE_PATH)
    )

    if not path.exists():
        print(
            f"error: no database at {path}; start HookWatch once to create it",
            file=sys.stderr,
        )
        return 1

    try:
        # An older database gains the principals table here, so the CLI works
        # against a file the current server has not opened yet.
        migrate(path)
        keys = KeyService(KeyRepository(path))

        if args.command == "create":
            issued = keys.create(args.name, args.role)
            print(format_issued(issued))
            print(
                "warning: this key is shown once and cannot be recovered; "
                "store it now",
                file=sys.stderr,
            )
        elif args.command == "list":
            stored = keys.list(include_revoked=args.all)
            if not stored:
                print("no API keys stored")
            for principal in stored:
                print(format_principal(principal))
        elif args.command == "rotate":
            issued = keys.rotate(args.name)
            print(format_issued(issued))
            print(
                "warning: the previous key is revoked and this key is shown "
                "once; update the client now",
                file=sys.stderr,
            )
        elif args.command == "revoke":
            principal = keys.revoke(args.name)
            print(
                json.dumps(
                    {
                        "name": principal.name,
                        "role": principal.role.value,
                        "revoked_at": (
                            principal.revoked_at.isoformat()
                            if principal.revoked_at is not None
                            else None
                        ),
                    },
                    ensure_ascii=False,
                )
            )
    except (ValueError, LookupError) as error:
        print(f"error: {error}", file=sys.stderr)
        return 1
    except sqlite3.Error as error:
        print(f"error: cannot use database {path}: {error}", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
