"""Database access for operator identities and their API keys.

All SQL for principals lives here, the same boundary the event repository
draws: the service and the HTTP and MCP interfaces never see a connection or a
table name.

The raw key never reaches this module. The service hashes it first, and only
the hash and a short display prefix are written.
"""

from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

from app.db import Principal, Role, connect


def _utc(value: datetime) -> datetime:
    return value.astimezone(timezone.utc)


class KeyRepository:
    """Stores and retrieves hashed API keys."""

    def __init__(self, database_path: Path) -> None:
        self.database_path = database_path

    def create(
        self,
        *,
        name: str,
        role: Role,
        key_prefix: str,
        key_hash: str,
        created_at: datetime | None = None,
    ) -> Principal:
        """Insert one active key.

        Raises sqlite3.IntegrityError when an active key already uses this name
        or hash. The service turns the name collision into a clear error; a
        hash collision cannot happen with 256 random bits and is not worth a
        second message.
        """
        created = _utc(created_at or datetime.now(timezone.utc))

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                INSERT INTO principals
                    (name, role, key_prefix, key_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, role.value, key_prefix, key_hash, created.isoformat()),
            )
            row_id = int(cursor.lastrowid)

        return Principal(
            id=row_id,
            name=name,
            role=role,
            key_prefix=key_prefix,
            created_at=created,
            last_used_at=None,
            revoked_at=None,
        )

    def rotate(
        self,
        *,
        name: str,
        key_prefix: str,
        key_hash: str,
        at: datetime | None = None,
    ) -> Principal | None:
        """Revoke the active key and insert its replacement in one transaction.

        Returns None when no active key has that name. Keeping both writes in
        one transaction means a failure cannot leave the name occupied by neither
        the old key nor its replacement. The role is carried over, so rotating
        never silently changes what the key may do.
        """
        revoked_at = _utc(at or datetime.now(timezone.utc))

        with closing(connect(self.database_path)) as connection, connection:
            row = connection.execute(
                """
                SELECT * FROM principals
                WHERE name = ? AND revoked_at IS NULL
                """,
                (name,),
            ).fetchone()
            if row is None:
                return None

            connection.execute(
                "UPDATE principals SET revoked_at = ? WHERE id = ?",
                (revoked_at.isoformat(), row["id"]),
            )
            cursor = connection.execute(
                """
                INSERT INTO principals
                    (name, role, key_prefix, key_hash, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (name, row["role"], key_prefix, key_hash, revoked_at.isoformat()),
            )
            row_id = int(cursor.lastrowid)
            role = Role(row["role"])

        return Principal(
            id=row_id,
            name=name,
            role=role,
            key_prefix=key_prefix,
            created_at=revoked_at,
            last_used_at=None,
            revoked_at=None,
        )

    def revoke(self, principal_id: int, at: datetime | None = None) -> bool:
        """Revoke one key, returning False when it was already revoked.

        The conditional UPDATE makes a second revoke detectable instead of a
        silent overwrite of the original timestamp.
        """
        revoked_at = _utc(at or datetime.now(timezone.utc))

        with closing(connect(self.database_path)) as connection, connection:
            cursor = connection.execute(
                """
                UPDATE principals
                SET revoked_at = ?
                WHERE id = ? AND revoked_at IS NULL
                """,
                (revoked_at.isoformat(), principal_id),
            )
            return cursor.rowcount == 1

    def find_active_by_hash(self, key_hash: str) -> Principal | None:
        """Return the active key with this hash, or None.

        Authentication is a lookup by hash, so the database index does the
        comparison. That is safe because the key is 256 random bits: an
        attacker cannot learn a useful prefix by timing a lookup.
        """
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT * FROM principals
                WHERE key_hash = ? AND revoked_at IS NULL
                """,
                (key_hash,),
            ).fetchone()

        return Principal.from_row(row) if row is not None else None

    def find_active_by_name(self, name: str) -> Principal | None:
        """Return the active key with this name, or None."""
        with closing(connect(self.database_path)) as connection:
            row = connection.execute(
                """
                SELECT * FROM principals
                WHERE name = ? AND revoked_at IS NULL
                """,
                (name,),
            ).fetchone()

        return Principal.from_row(row) if row is not None else None

    def touch(self, principal_id: int, at: datetime | None = None) -> None:
        """Record that a key was used, for operator visibility."""
        used_at = _utc(at or datetime.now(timezone.utc))

        with closing(connect(self.database_path)) as connection, connection:
            connection.execute(
                "UPDATE principals SET last_used_at = ? WHERE id = ?",
                (used_at.isoformat(), principal_id),
            )

    def list(self, *, include_revoked: bool = False) -> list[Principal]:
        """Return keys, oldest first.

        Revoked keys are hidden by default because they cannot be used; the
        audit trail they left in replays still names them, so the rows are kept.
        """
        where = "" if include_revoked else "WHERE revoked_at IS NULL"

        with closing(connect(self.database_path)) as connection:
            rows = connection.execute(
                f"SELECT * FROM principals {where} ORDER BY id"
            ).fetchall()

        return [Principal.from_row(row) for row in rows]
