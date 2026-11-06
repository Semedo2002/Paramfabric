from pathlib import Path

from sqlalchemy import inspect

from paramfabric.cli import database_upgrade
from paramfabric.governance.store import create_database


def test_database_upgrade_creates_versioned_schema(tmp_path, monkeypatch):
    database = tmp_path / "migrated.db"
    monkeypatch.setenv("PARAMFABRIC_DATABASE_URL", f"sqlite:///{database.as_posix()}")

    database_upgrade()

    engine, _ = create_database(
        f"sqlite:///{database.as_posix()}",
        create_schema=False,
    )
    try:
        names = set(inspect(engine).get_table_names())
    finally:
        engine.dispose()
    assert {
        "alembic_version",
        "parameter_change_requests",
        "parameter_change_reviews",
        "parameter_baselines",
        "parameter_fabric_states",
    } <= names
    assert "base_fabric" in {column["name"] for column in inspect(engine).get_columns("parameter_change_requests")}


def test_multi_tenant_migration_preserves_existing_rows(tmp_path, monkeypatch):
    import sqlite3

    from alembic import command
    from alembic.config import Config

    import paramfabric

    database = tmp_path / "existing.db"
    monkeypatch.setenv("PARAMFABRIC_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    config = Config()
    config.set_main_option("script_location", str(Path(paramfabric.__file__).parent / "migrations"))
    command.upgrade(config, "0002_request_base_snapshot")
    with sqlite3.connect(database) as connection:
        connection.execute("INSERT INTO parameter_baselines (tag, fabric_hash, fabric_json, created_by, created_at) VALUES ('v1', 'h', '{}', 'lead', '2026-01-01')")
        connection.execute("INSERT INTO parameter_fabric_states (branch, fabric_json, updated_by, updated_at) VALUES ('main', '{}', 'lead', '2026-01-01')")
        connection.execute("INSERT INTO parameter_change_requests (id, author, rationale, target_branch, base_hash, status, proposed_fabric, created_at) "
                           "VALUES ('r1', 'eng', 'why', 'main', 'h', 'merged', '{}', '2026-01-01')")
    command.upgrade(config, "head")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT organization_id, project_id, tag FROM parameter_baselines").fetchall() == [("default", "default", "v1")]
        assert connection.execute("SELECT organization_id, project_id FROM parameter_change_requests").fetchall() == [("default", "default")]
        assert connection.execute("SELECT id FROM organizations").fetchall() == [("default",)]
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            connection.execute("INSERT INTO parameter_fabric_states (organization_id, project_id, branch, fabric_json, updated_by) VALUES ('other', 'default', 'main', '{}', 'x')")
            raise AssertionError("cross-tenant row accepted")
        except sqlite3.IntegrityError:
            pass
    command.downgrade(config, "0002_request_base_snapshot")
    with sqlite3.connect(database) as connection:
        assert connection.execute("SELECT tag FROM parameter_baselines").fetchall() == [("v1",)]
        assert "organizations" not in {row[0] for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def test_signing_keys_migration_matches_the_model(tmp_path, monkeypatch):
    from sqlalchemy import text

    database = tmp_path / "keys.db"
    monkeypatch.setenv("PARAMFABRIC_DATABASE_URL", f"sqlite:///{database.as_posix()}")
    database_upgrade()
    engine, _ = create_database(f"sqlite:///{database.as_posix()}", create_schema=False)
    try:
        inspector = inspect(engine)
        columns = {column["name"] for column in inspector.get_columns("signing_keys")}
        assert {"organization_id", "key_id", "algorithm", "public_key_pem", "purpose", "created_at", "revoked_at"} <= columns
        assert inspector.get_pk_constraint("signing_keys")["constrained_columns"] == ["organization_id", "key_id"]
        assert any(fk["referred_table"] == "organizations" for fk in inspector.get_foreign_keys("signing_keys"))
        with engine.connect() as connection:
            assert connection.execute(text("select version_num from alembic_version")).scalar() == "0004_signing_keys"
    finally:
        engine.dispose()
