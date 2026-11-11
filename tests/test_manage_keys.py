import json
import sqlite3
from pathlib import Path

import pytest

from app.db import migrate
from tools.manage_keys import main


def test_create_prints_the_key_once(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)

    exit_code = main(
        [
            "create",
            "--db",
            str(database_path),
            "--name",
            "hermes",
            "--role",
            "operator",
        ]
    )

    captured = capsys.readouterr()
    issued = json.loads(captured.out)
    assert exit_code == 0
    assert issued["name"] == "hermes"
    assert issued["role"] == "operator"
    assert issued["key"].startswith("hw_")
    assert issued["prefix"] == issued["key"][: len("hw_") + 8]
    assert "shown once" in captured.err


def test_create_adds_the_principals_table_to_an_older_database(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    # A v5 database has no principals table until the CLI migrates it.
    migrate(database_path)
    with sqlite3.connect(database_path) as connection:
        connection.execute("DROP TABLE principals")
        connection.execute("PRAGMA user_version = 5")

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--name",
                "hermes",
                "--role",
                "operator",
            ]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["name"] == "hermes"


def test_create_refuses_a_missing_database(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        main(
            [
                "create",
                "--db",
                str(tmp_path / "missing.db"),
                "--name",
                "hermes",
                "--role",
                "operator",
            ]
        )
        == 1
    )
    assert "no database at" in capsys.readouterr().err


def test_create_refuses_a_duplicate_name(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)
    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--name",
                "hermes",
                "--role",
                "operator",
            ]
        )
        == 0
    )
    capsys.readouterr()

    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--name",
                "hermes",
                "--role",
                "viewer",
            ]
        )
        == 1
    )
    assert "already exists" in capsys.readouterr().err


def test_list_prints_keys_without_secrets(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)
    assert (
        main(
            [
                "create",
                "--db",
                str(database_path),
                "--name",
                "hermes",
                "--role",
                "operator",
            ]
        )
        == 0
    )
    key = json.loads(capsys.readouterr().out)["key"]

    assert main(["list", "--db", str(database_path)]) == 0

    captured = capsys.readouterr()
    listed = json.loads(captured.out)
    assert listed["name"] == "hermes"
    assert listed["role"] == "operator"
    assert listed["prefix"] == key[: len("hw_") + 8]
    assert listed["last_used_at"] is None
    assert listed["revoked_at"] is None
    assert key not in captured.out


def test_list_reports_an_empty_database(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)

    assert main(["list", "--db", str(database_path)]) == 0
    assert "no API keys stored" in capsys.readouterr().out


def test_list_hides_revoked_keys_unless_all_is_requested(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)
    main(
        [
            "create",
            "--db",
            str(database_path),
            "--name",
            "old",
            "--role",
            "operator",
        ]
    )
    main(
        [
            "create",
            "--db",
            str(database_path),
            "--name",
            "current",
            "--role",
            "viewer",
        ]
    )
    capsys.readouterr()
    assert main(["revoke", "--db", str(database_path), "--name", "old"]) == 0
    capsys.readouterr()

    assert main(["list", "--db", str(database_path)]) == 0
    active = [
        json.loads(line)["name"]
        for line in capsys.readouterr().out.strip().splitlines()
    ]

    assert main(["list", "--db", str(database_path), "--all"]) == 0
    everything = [
        json.loads(line)["name"]
        for line in capsys.readouterr().out.strip().splitlines()
    ]

    assert active == ["current"]
    assert everything == ["old", "current"]


def test_rotate_returns_a_new_key_and_revokes_the_old_one(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)
    main(
        [
            "create",
            "--db",
            str(database_path),
            "--name",
            "hermes",
            "--role",
            "operator",
        ]
    )
    old = json.loads(capsys.readouterr().out)["key"]

    assert main(["rotate", "--db", str(database_path), "--name", "hermes"]) == 0

    captured = capsys.readouterr()
    new = json.loads(captured.out)["key"]
    assert new != old
    assert "previous key is revoked" in captured.err

    assert main(["list", "--db", str(database_path)]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "hermes"


def test_rotate_refuses_an_unknown_name(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)

    assert main(["rotate", "--db", str(database_path), "--name", "missing"]) == 1
    assert "no active key" in capsys.readouterr().err


def test_revoke_reports_the_time(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)
    main(
        [
            "create",
            "--db",
            str(database_path),
            "--name",
            "hermes",
            "--role",
            "operator",
        ]
    )
    capsys.readouterr()

    assert main(["revoke", "--db", str(database_path), "--name", "hermes"]) == 0

    revoked = json.loads(capsys.readouterr().out)
    assert revoked["name"] == "hermes"
    assert revoked["revoked_at"] is not None


def test_revoke_twice_fails(
    database_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    migrate(database_path)
    main(
        [
            "create",
            "--db",
            str(database_path),
            "--name",
            "hermes",
            "--role",
            "operator",
        ]
    )
    assert main(["revoke", "--db", str(database_path), "--name", "hermes"]) == 0
    capsys.readouterr()

    assert main(["revoke", "--db", str(database_path), "--name", "hermes"]) == 1
    assert "no active key" in capsys.readouterr().err
