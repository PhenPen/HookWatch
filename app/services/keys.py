"""Operator identities, API key issuance and authentication.

HTTP and MCP both authenticate through this service, so a key behaves the same
way in both interfaces. Only a SHA-256 hash of each key is stored: the raw key
is returned once, when it is created or rotated, and cannot be recovered from
the database afterwards.

Why SHA-256 and not a password hash: a key is 256 bits of randomness generated
here, not a human-chosen password. There is nothing to brute force and no need
for a slow, salted hash, which would make every authenticated request more
expensive for no benefit. A salted hash would also make lookup by hash
impossible without scanning every row.

Roles are hierarchical: operator includes viewer. Replay is the only operation
that requires operator; reads require viewer.
"""

import hashlib
import logging
import re
import secrets
import sqlite3
from dataclasses import dataclass, replace
from datetime import datetime, timezone

from app.db import Principal, Role
from app.repositories.keys import KeyRepository

logger = logging.getLogger(__name__)

KEY_PREFIX = "hw_"
KEY_BYTES = 32
# Enough of the key to recognise one in a listing, never enough to use it.
PREFIX_LENGTH = len(KEY_PREFIX) + 8
NAME_PATTERN = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


class KeyNameTakenError(ValueError):
    """An active key already uses this name."""


class UnknownKeyNameError(LookupError):
    """No active key has this name."""


@dataclass(frozen=True)
class IssuedKey:
    """A key and its identity, returned only at creation or rotation."""

    principal: Principal
    key: str


def _hash_key(raw_key: str) -> str:
    """Return the hex SHA-256 of a key, which is what the database stores."""
    return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()


def _new_key() -> str:
    """Generate a key with 256 random bits, URL-safe so it survives a shell."""
    return KEY_PREFIX + secrets.token_urlsafe(KEY_BYTES)


class KeyService:
    """Issue, rotate, revoke and authenticate operator API keys."""

    def __init__(self, repository: KeyRepository) -> None:
        self.repository = repository

    def create(
        self,
        name: str,
        role: Role | str,
        *,
        at: datetime | None = None,
    ) -> IssuedKey:
        """Issue an active key and return it once.

        The name is an identity, not a credential: it appears in the replay
        audit trail and in listings, so it has the same shape as a source name.
        Reusing a name while its key is active is refused rather than silently
        creating two identities that look the same in history.
        """
        validated_name = _validate_name(name)
        validated_role = _validate_role(role)
        key = _new_key()

        try:
            principal = self.repository.create(
                name=validated_name,
                role=validated_role,
                key_prefix=key[:PREFIX_LENGTH],
                key_hash=_hash_key(key),
                created_at=at,
            )
        except sqlite3.IntegrityError:
            raise KeyNameTakenError(
                f"an active key named {validated_name!r} already exists"
            ) from None

        logger.info(
            "issued API key name=%s role=%s",
            principal.name,
            principal.role.value,
        )
        return IssuedKey(principal=principal, key=key)

    def rotate(
        self,
        name: str,
        *,
        at: datetime | None = None,
    ) -> IssuedKey:
        """Replace the active key for a name and return the new key once.

        The old key stops working immediately and its row is kept, so replay
        history written under this name still makes sense.
        """
        validated_name = _validate_name(name)
        key = _new_key()

        principal = self.repository.rotate(
            name=validated_name,
            key_prefix=key[:PREFIX_LENGTH],
            key_hash=_hash_key(key),
            at=at,
        )
        if principal is None:
            raise UnknownKeyNameError(
                f"no active key named {validated_name!r}"
            )

        logger.info(
            "rotated API key name=%s role=%s",
            principal.name,
            principal.role.value,
        )
        return IssuedKey(principal=principal, key=key)

    def revoke(self, name: str, *, at: datetime | None = None) -> Principal:
        """Revoke the active key for a name.

        Revocation is immediate for HTTP, which authenticates each request, and
        takes effect on the next call for MCP, which re-checks the key per
        replay.
        """
        validated_name = _validate_name(name)
        principal = self.repository.find_active_by_name(validated_name)
        if principal is None:
            raise UnknownKeyNameError(
                f"no active key named {validated_name!r}"
            )

        revoked_at = (at or datetime.now(timezone.utc)).astimezone(timezone.utc)
        if not self.repository.revoke(principal.id, at=revoked_at):
            raise UnknownKeyNameError(
                f"key named {validated_name!r} was revoked by another process"
            )

        logger.info(
            "revoked API key name=%s role=%s",
            principal.name,
            principal.role.value,
        )
        return replace(principal, revoked_at=revoked_at)

    def list(self, *, include_revoked: bool = False) -> list[Principal]:
        """Return keys for an operator listing, active ones by default."""
        return self.repository.list(include_revoked=include_revoked)

    def authenticate(
        self,
        raw_key: str | None,
        *,
        at: datetime | None = None,
    ) -> Principal | None:
        """Return the principal for a valid active key, or None.

        Whitespace around the supplied key is ignored so a key pasted into an
        environment variable with a trailing newline still works. A key can
        never contain whitespace itself. A successful lookup records the use,
        which is what makes an unused key visible in the listing.
        """
        if raw_key is None:
            return None
        key = raw_key.strip()
        if not key:
            return None

        principal = self.repository.find_active_by_hash(_hash_key(key))
        if principal is None:
            return None

        self.repository.touch(principal.id, at=at)
        return principal


def _validate_name(name: str) -> str:
    """Normalise and check the operator-visible identity."""
    if not isinstance(name, str):
        raise ValueError("key name must be a string")
    validated = name.strip()
    if not NAME_PATTERN.fullmatch(validated):
        raise ValueError(
            "key name must be 1 to 64 characters of letters, digits, dot, "
            f"underscore or hyphen, got {name!r}"
        )
    return validated


def _validate_role(role: Role | str) -> Role:
    """Accept a Role or its stored spelling, rejecting anything else."""
    try:
        return Role(role)
    except ValueError:
        raise ValueError(
            "role must be one of viewer or operator, "
            f"got {role!r}"
        ) from None
