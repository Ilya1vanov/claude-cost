"""Store schema version and migrations (ticket #118, ADR-0008): real SQLite files, store and CLI seams."""
import io
import sqlite3

import pytest

from claude_cost import cli, releases, store


def make_unversioned_store(path):
    """A store in the shape from before schema versions: Spotlights keyed by name, no Locked column,
    Subagent compactions keyed by uuid alone, user_version 0."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(store.SCHEMA)
    conn.executescript("""
        DROP TABLE spotlights;
        CREATE TABLE spotlights (name TEXT NOT NULL, rule TEXT NOT NULL DEFAULT '');
        ALTER TABLE auto_spotlights DROP COLUMN locked;
        DROP TABLE subagent_compactions;
        CREATE TABLE subagent_compactions (uuid TEXT PRIMARY KEY, stem TEXT NOT NULL,
            agent_id TEXT NOT NULL, ts_epoch REAL NOT NULL);
        INSERT INTO spotlights (name) VALUES ('Sprint');
        INSERT INTO spotlight_pins VALUES ('Sprint', 'sess-1');
        INSERT INTO spotlight_includes VALUES ('Sprint', 'PROJ-1');
        INSERT INTO auto_spotlights (key, name) VALUES ('PROJ-1', 'Renamed');
        PRAGMA user_version = 0;
    """)
    conn.commit()
    conn.close()
    return path


def version(path):
    conn = sqlite3.connect(path)
    try:
        return conn.execute("PRAGMA user_version").fetchone()[0]
    finally:
        conn.close()


def test_unversioned_store_migrates_to_latest_keeping_user_data(tmp_path):
    path = make_unversioned_store(tmp_path / "store.sqlite")
    conn = store.connect(path)
    sid = conn.execute("SELECT id FROM spotlights WHERE name = 'Sprint'").fetchone()[0]
    def rows(sql, *args):
        return [tuple(r) for r in conn.execute(sql, args)]
    assert rows("SELECT session_id FROM spotlight_pins WHERE spotlight = ?", sid) == [("sess-1",)]
    assert rows("SELECT included FROM spotlight_includes WHERE spotlight = ?", sid) == [("auto:PROJ-1",)]
    assert rows("SELECT name, locked FROM auto_spotlights") == [("Renamed", 0)]
    conn.close()
    assert version(path) == store.LATEST_VERSION >= 1


def test_a_failing_step_leaves_the_store_byte_for_byte_unchanged(tmp_path, monkeypatch):
    path = make_unversioned_store(tmp_path / "store.sqlite")
    before = path.read_bytes()

    def broken(conn):
        conn.execute("INSERT INTO spotlight_pins VALUES ('x', 'y')")
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "MIGRATIONS", [*store.MIGRATIONS, broken])
    monkeypatch.setattr(store, "LATEST_VERSION", len(store.MIGRATIONS))
    with pytest.raises(store.MigrationError, match="boom"):
        store.connect(path)
    assert path.read_bytes() == before


def test_a_store_newer_than_the_code_is_refused(tmp_path):
    path = tmp_path / "store.sqlite"
    store.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {store.LATEST_VERSION + 1}")
    raw.close()
    before = path.read_bytes()
    with pytest.raises(store.StoreTooNewError, match="newer"):
        store.connect(path)
    assert path.read_bytes() == before


def run(argv, env):
    out, err = io.StringIO(), io.StringIO()
    code = cli.main(argv, env=env, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def test_migrate_command_migrates_the_configured_store_and_is_idempotent(env, tmp_path):
    path = make_unversioned_store(tmp_path / "data" / "store.sqlite")
    code, out, _ = run(["migrate"], env)
    assert code == 0 and "version" in out and version(path) == store.LATEST_VERSION
    code, out, _ = run(["migrate"], env)
    assert code == 0 and "up to date" in out


def test_migrate_command_fails_non_zero_with_a_message_and_the_reinstall_command(env, tmp_path, monkeypatch):
    path = make_unversioned_store(tmp_path / "data" / "store.sqlite")
    before = path.read_bytes()

    def broken(conn):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "MIGRATIONS", [*store.MIGRATIONS, broken])
    monkeypatch.setattr(store, "LATEST_VERSION", len(store.MIGRATIONS))
    code, _, err = run(["migrate"], env)
    assert code == 1 and "boom" in err and "unchanged" in err and "uv tool install" in err
    assert path.read_bytes() == before


def test_other_commands_migrate_an_outdated_store_first(env, tmp_path):
    path = make_unversioned_store(tmp_path / "data" / "store.sqlite")
    code, out, _ = run(["today"], env)
    assert code == 0 and out.startswith("Today (")
    assert version(path) == store.LATEST_VERSION


def test_other_commands_refuse_a_store_newer_than_the_code(env, tmp_path):
    path = tmp_path / "data" / "store.sqlite"
    store.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute(f"PRAGMA user_version = {store.LATEST_VERSION + 1}")
    raw.close()
    code, out, err = run(["today"], env)
    assert code == 1 and "newer" in err and out == ""


def test_migrate_failure_hint_names_the_wheel_of_the_version_updated_from(env, tmp_path, monkeypatch):
    path = make_unversioned_store(tmp_path / "data" / "store.sqlite")
    path.with_name("store.sqlite.0.1.0.bak").write_text("b")

    def broken(conn):
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "MIGRATIONS", [*store.MIGRATIONS, broken])
    monkeypatch.setattr(store, "LATEST_VERSION", len(store.MIGRATIONS))
    code, _, err = run(["migrate"], env)
    assert code == 1 and releases.wheel_url_for("0.1.0") in err


def test_schema_version_reads_a_store_without_opening_it_for_migration(tmp_path):
    assert store.schema_version(tmp_path / "missing.sqlite") == 0
    path = make_unversioned_store(tmp_path / "s.sqlite")
    assert store.schema_version(path) == 0
    store.connect(path).close()
    assert store.schema_version(path) == store.LATEST_VERSION


def test_a_script_ending_without_a_semicolon_is_refused():
    with pytest.raises(ValueError, match="unterminated"):
        list(store._statements("CREATE TABLE a (x);\nCREATE TABLE b (y)"))


def test_a_failing_v2_step_rolls_the_store_back_to_version_1_unchanged(tmp_path, monkeypatch):
    path = tmp_path / "store.sqlite"
    old = sqlite3.connect(path)  # the store as the previous release left it
    old.row_factory = sqlite3.Row
    old.isolation_level = None
    old.execute("BEGIN")
    store.MIGRATIONS[0](old)
    old.execute("PRAGMA user_version = 1")
    old.execute("COMMIT")
    old.close()
    before = path.read_bytes()
    real_v2 = store.MIGRATIONS[1]

    def failing_v2(conn):
        real_v2(conn)  # applies its changes, then dies before the end
        raise RuntimeError("boom")
    monkeypatch.setattr(store, "MIGRATIONS", [store.MIGRATIONS[0], failing_v2])
    monkeypatch.setattr(store, "LATEST_VERSION", 2)
    with pytest.raises(store.MigrationError, match="boom"):
        store.connect(path)
    assert path.read_bytes() == before
    assert version(path) == 1
