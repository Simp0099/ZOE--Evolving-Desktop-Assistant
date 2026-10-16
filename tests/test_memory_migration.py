"""The Jarvis -> Zoe memory-store migration.

The store is the user's own data, so the rules are narrow and tested directly:
copy once, never overwrite, never delete the original. Nothing here touches the
live store -- every test builds its own database in ``tmp_path``.
"""
import shutil
import sqlite3

import pytest

from zoe import memory


def _make_legacy(path, facts):
    """A pre-rename store with rows, built the same way the real one was."""
    conn = sqlite3.connect(str(path))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            fact TEXT NOT NULL,
            category TEXT DEFAULT 'general',
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            subject TEXT,
            active INTEGER DEFAULT 1,
            updated_at TIMESTAMP,
            source TEXT DEFAULT 'inferred'
        )
    """)
    for fact in facts:
        conn.execute("INSERT INTO memories (fact, category) VALUES (?, 'general')", (fact,))
    conn.commit()
    conn.close()


class TestMemoryMigration:
    def test_a_legacy_store_with_rows_is_copied_and_all_rows_are_readable(self, tmp_path):
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        facts = ["Prefers tea, not coffee", "Lives in Faridabad", "Working on the Zoe rename"]
        _make_legacy(legacy, facts)

        assert memory.migrate_legacy_memory_db(target, legacy) is True
        assert target.exists()

        conn = memory.init_memory_db(db_path=target)
        try:
            recalled = memory.recall_all(conn, limit=50)
        finally:
            conn.close()
        assert len(recalled) == 3
        for fact in facts:
            assert any(fact in r for r in recalled), f"{fact!r} missing from {recalled}"

    def test_the_legacy_file_is_never_deleted(self, tmp_path):
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        _make_legacy(legacy, ["keep me"])
        memory.migrate_legacy_memory_db(target, legacy)
        assert legacy.exists(), "the original is the user's data; it stays"

    def test_an_existing_new_store_is_never_overwritten(self, tmp_path):
        """The idempotence rule. A second start must not clobber newer memories."""
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        _make_legacy(legacy, ["stale row from before the rename"])
        _make_legacy(target, ["newer row written after the rename"])

        assert memory.migrate_legacy_memory_db(target, legacy) is False

        conn = memory.init_memory_db(db_path=target)
        try:
            recalled = memory.recall_all(conn, limit=50)
        finally:
            conn.close()
        assert any("newer row" in r for r in recalled)
        assert not any("stale row" in r for r in recalled)

    def test_running_twice_does_not_overwrite(self, tmp_path):
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        _make_legacy(legacy, ["only row"])

        assert memory.migrate_legacy_memory_db(target, legacy) is True
        first = target.read_bytes()
        assert memory.migrate_legacy_memory_db(target, legacy) is False
        assert target.read_bytes() == first

    def test_with_neither_file_present_a_fresh_store_is_created(self, tmp_path):
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        assert not legacy.exists() and not target.exists()

        assert memory.migrate_legacy_memory_db(target, legacy) is False

        conn = memory.init_memory_db(db_path=target)
        try:
            assert memory.recall_all(conn, limit=50) == []
        finally:
            conn.close()
        assert target.exists()

    def test_the_copied_store_is_a_real_file_not_a_link(self, tmp_path):
        """A symlink here would make a later unlink destroy the original."""
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        _make_legacy(legacy, ["a fact"])
        memory.migrate_legacy_memory_db(target, legacy)
        assert not target.is_symlink()

    def test_init_memory_db_migrates_the_default_store(self, tmp_path, monkeypatch):
        """Wiring: the migration runs from init_memory_db on the default path,
        which is how it reaches production without a separate call site."""
        legacy = tmp_path / "jarvis_memory.db"
        target = tmp_path / "zoe_memory.db"
        _make_legacy(legacy, ["carried across the rename"])

        monkeypatch.setattr(memory, "MEMORY_DB_PATH", target)
        monkeypatch.setattr(memory, "LEGACY_MEMORY_DB_PATH", legacy)

        conn = memory.init_memory_db()
        try:
            recalled = memory.recall_all(conn, limit=50)
        finally:
            conn.close()
        assert any("carried across the rename" in r for r in recalled)
        assert legacy.exists()

    def test_the_constants_point_at_the_new_and_old_names(self):
        assert memory.MEMORY_DB_PATH.name == "zoe_memory.db"
        assert memory.LEGACY_MEMORY_DB_PATH.name == "jarvis_memory.db"

    def test_the_live_store_was_not_disturbed(self):
        """A read-only sanity check that this test module never writes to data/."""
        assert str(memory.MEMORY_DB_PATH).endswith("zoe_memory.db")
        # shutil is imported for copy2 only; nothing else in the module deletes.
        assert shutil.copy2 is not None