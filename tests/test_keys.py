import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.db import Role, migrate
from app.repositories.keys import KeyRepository
from app.services.keys import (
    KEY_PREFIX,
    KeyNameTakenError,
    KeyService,
    UnknownKeyNameError,
)

NOW = datetime(2026, 11, 11, 12, 0, tzinfo=timezone.utc)


@pytest.fixture
def service(database_path: Path) -> KeyService:
    migrate(database_path)
    return KeyService(KeyRepository(database_path))


def read_principals(database_path: Path) -> list[sqlite3.Row]:
    with sqlite3.connect(database_path) as connection:
        connection.row_factory = sqlite3.Row
        return connection.execute("SELECT * FROM principals ORDER BY id").fetchall()


def test_an_issued_key_has_the_documented_shape(service: KeyService) -> None:
    issued = service.create("hermes", Role.OPERATOR, at=NOW)

    assert issued.key.startswith(KEY_PREFIX)
    assert len(issued.key) > 40
    assert issued.principal.name == "hermes"
    assert issued.principal.role is Role.OPERATOR
    assert issued.principal.key_prefix == issued.key[: len(KEY_PREFIX) + 8]
    assert issued.principal.created_at == NOW
    assert issued.principal.last_used_at is None
    assert issued.principal.revoked_at is None


def test_the_database_stores_only_a_hash(
    service: KeyService,
    database_path: Path,
) -> None:
    issued = service.create("hermes", Role.OPERATOR)

    rows = read_principals(database_path)

    assert len(rows) == 1
    assert rows[0]["key_hash"] == hashlib.sha256(
        issued.key.encode("utf-8")
    ).hexdigest()
    assert rows[0]["key_prefix"] == issued.key[: len(KEY_PREFIX) + 8]
    assert issued.key.encode("utf-8") not in database_path.read_bytes()


def test_issued_keys_are_unique(service: KeyService) -> None:
    first = service.create("first", Role.VIEWER).key
    second = service.create("second", Role.VIEWER).key

    assert first != second


def test_an_active_name_cannot_be_issued_twice(service: KeyService) -> None:
    service.create("hermes", Role.OPERATOR)

    with pytest.raises(KeyNameTakenError, match="already exists"):
        service.create("hermes", Role.VIEWER)


def test_a_name_can_be_reused_after_revocation(service: KeyService) -> None:
    old = service.create("hermes", Role.OPERATOR)
    service.revoke("hermes", at=NOW)

    new = service.create("hermes", Role.VIEWER, at=NOW)

    assert old.key != new.key
    assert service.authenticate(old.key) is None
    assert service.authenticate(new.key) is not None


@pytest.mark.parametrize(
    "name",
    ["", "   ", "bad name", "a" * 65, "quote\"name", "semi;colon"],
)
def test_impossible_names_are_rejected(service: KeyService, name: str) -> None:
    with pytest.raises(ValueError, match="key name"):
        service.create(name, Role.OPERATOR)


def test_the_name_is_normalised(service: KeyService) -> None:
    issued = service.create("  hermes  ", Role.OPERATOR)

    assert issued.principal.name == "hermes"


@pytest.mark.parametrize("role", ["owner", "", "Operator "])
def test_unknown_roles_are_rejected(service: KeyService, role: str) -> None:
    with pytest.raises(ValueError, match="role must be one of"):
        service.create("hermes", role)


def test_the_stored_role_spelling_is_accepted(service: KeyService) -> None:
    issued = service.create("reader", "viewer")

    assert issued.principal.role is Role.VIEWER


def test_authentication_returns_the_principal(service: KeyService) -> None:
    issued = service.create("hermes", Role.OPERATOR)

    principal = service.authenticate(issued.key)

    assert principal is not None
    assert principal.id == issued.principal.id
    assert principal.name == "hermes"
    assert principal.role is Role.OPERATOR


@pytest.mark.parametrize("raw", [None, "", "   ", "hw_unknown"])
def test_authentication_rejects_missing_and_unknown_keys(
    service: KeyService,
    raw: str | None,
) -> None:
    assert service.authenticate(raw) is None


def test_authentication_tolerates_surrounding_whitespace(
    service: KeyService,
) -> None:
    issued = service.create("hermes", Role.OPERATOR)

    assert service.authenticate(f"  {issued.key}\n") is not None


def test_authentication_rejects_a_revoked_key(service: KeyService) -> None:
    issued = service.create("hermes", Role.OPERATOR)
    service.revoke("hermes", at=NOW)

    assert service.authenticate(issued.key) is None


def test_authentication_records_the_last_use(service: KeyService) -> None:
    issued = service.create("hermes", Role.OPERATOR)

    service.authenticate(issued.key, at=NOW)
    principal = service.list()[0]

    assert principal.last_used_at == NOW


def test_rotation_replaces_the_key_and_keeps_the_identity(
    service: KeyService,
) -> None:
    old = service.create("hermes", Role.OPERATOR)

    new = service.rotate("hermes", at=NOW + timedelta(hours=1))

    assert new.key != old.key
    assert new.principal.id != old.principal.id
    assert new.principal.name == "hermes"
    assert new.principal.role is Role.OPERATOR
    assert new.principal.created_at == NOW + timedelta(hours=1)
    assert service.authenticate(old.key) is None
    assert service.authenticate(new.key) is not None


def test_rotation_keeps_the_old_row_for_the_audit_trail(
    service: KeyService,
    database_path: Path,
) -> None:
    service.create("hermes", Role.OPERATOR)
    service.rotate("hermes", at=NOW)

    rows = read_principals(database_path)

    assert len(rows) == 2
    assert rows[0]["revoked_at"] == NOW.isoformat()
    assert rows[1]["revoked_at"] is None


def test_rotation_of_an_unknown_name_fails(service: KeyService) -> None:
    with pytest.raises(UnknownKeyNameError, match="no active key"):
        service.rotate("missing")


def test_revoking_a_key_records_the_time(service: KeyService) -> None:
    service.create("hermes", Role.OPERATOR)

    revoked = service.revoke("hermes", at=NOW)

    assert revoked.name == "hermes"
    assert revoked.revoked_at == NOW


def test_revoking_twice_is_refused(service: KeyService) -> None:
    service.create("hermes", Role.OPERATOR)
    service.revoke("hermes", at=NOW)

    with pytest.raises(UnknownKeyNameError, match="no active key"):
        service.revoke("hermes")


def test_revoking_an_unknown_name_fails(service: KeyService) -> None:
    with pytest.raises(UnknownKeyNameError, match="no active key"):
        service.revoke("missing")


def test_revoking_one_key_leaves_the_others_working(
    service: KeyService,
) -> None:
    revoked = service.create("old", Role.OPERATOR)
    kept = service.create("current", Role.OPERATOR)

    service.revoke("old")

    assert service.authenticate(revoked.key) is None
    assert service.authenticate(kept.key) is not None


def test_listing_hides_revoked_keys_by_default(service: KeyService) -> None:
    service.create("active", Role.VIEWER)
    service.create("gone", Role.OPERATOR)
    service.revoke("gone", at=NOW)

    assert [principal.name for principal in service.list()] == ["active"]
    assert [principal.name for principal in service.list(include_revoked=True)] == [
        "active",
        "gone",
    ]


def test_operator_covers_viewer_but_not_the_reverse() -> None:
    assert Role.OPERATOR.covers(Role.VIEWER)
    assert Role.OPERATOR.covers(Role.OPERATOR)
    assert Role.VIEWER.covers(Role.VIEWER)
    assert not Role.VIEWER.covers(Role.OPERATOR)
